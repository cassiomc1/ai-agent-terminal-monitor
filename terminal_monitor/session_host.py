"""Detached PTY session host: owns one agent PTY, replay, and local control socket.

The host is deliberately small: no classification, Git, PR, policy, or remote
logic. It owns the PTY master and child process, keeps bounded in-memory
replay, and exposes an authenticated owner-only Unix-socket control plane.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import json
import os
import queue
import secrets
import selectors
import signal
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .replay import ReplayBuffer
from .safety import redact_sensitive
from .session_protocol import (
    MAX_CONTROL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SNAPSHOT_CHUNK_BYTES,
    SessionProtocolError,
    receive_message,
    send_message,
)
from .state import _atomic_json_write, now_iso

SESSION_METADATA_NAME = "managed-session.json"
SESSION_TOKEN_NAME = "session-token"
SESSION_SOCKET_NAME = "session-control.sock"
SCHEMA_VERSION = 1
STREAM_QUEUE_MAX_ITEMS = 64
TERMINATE_GRACE_SECONDS = 5.0
# Bounded grace for in-flight control connections to flush their final frame
# (notably the stream "exit" event) before the host process exits.
FINAL_FLUSH_SECONDS = 2.0
# Bounded wait for the reaper/reader to record the exit code and drain the PTY
# before an explicit terminate forces finalization.
REAPER_HANDOFF_SECONDS = 2.0


@dataclass(frozen=True)
class SessionHostConfig:
    session_id: str
    command: tuple[str, ...]
    cwd: str
    state_dir: str
    cols: int = 120
    rows: int = 36
    replay_bytes: int = 512 * 1024


def _resolved_inside(state_dir: Path, name: str) -> Path:
    base = state_dir.resolve()
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"refusing path outside state directory: {name!r}") from exc
    return target


def _metadata_path(state_dir: str) -> Path:
    return _resolved_inside(Path(state_dir), SESSION_METADATA_NAME)


def _token_path(state_dir: str) -> Path:
    return _resolved_inside(Path(state_dir), SESSION_TOKEN_NAME)


def _socket_path(state_dir: str) -> Path:
    return _resolved_inside(Path(state_dir), SESSION_SOCKET_NAME)


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_metadata_dict(state_dir: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_metadata_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _try_authenticated_status(state_dir: str, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        sock_path = _socket_path(state_dir)
        token_file = _token_path(state_dir)
        if not sock_path.exists() or not token_file.is_file():
            return None
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            return None
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            client.connect(str(sock_path))
            send_message(client, {"version": PROTOCOL_VERSION, "token": token, "op": "status"})
            resp = receive_message(client)
        finally:
            with contextlib.suppress(OSError):
                client.close()
        if isinstance(resp, dict) and resp.get("ok"):
            return resp
        return None
    except (OSError, SessionProtocolError, ValueError, UnicodeDecodeError):
        return None


def classify_startup_state(state_dir: str) -> tuple[str, dict[str, Any] | None]:
    """Decide whether a new SessionHost may take ownership of ``state_dir``.

    Returns ``(decision, metadata)`` where decision is one of:

    - ``absent``: no metadata; safe to create.
    - ``live``: an authenticated host answers; adopt instead of creating.
    - ``stale``: metadata exists but the recorded host PID is provably dead.
    - ``uncertain``: metadata exists, the host cannot be reached, but the
      recorded PID may still be alive. Callers must FAIL CLOSED: never delete
      ownership artifacts and never start a second agent.
    """
    meta = _read_metadata_dict(state_dir)
    if meta is None:
        meta_path = _metadata_path(state_dir)
        if meta_path.exists() or meta_path.is_symlink():
            return "uncertain", None
        return "absent", None
    live = _try_authenticated_status(state_dir)
    if live and live.get("session_id"):
        return "live", meta
    raw_pid = meta.get("host_pid")
    try:
        host_pid = int(raw_pid) if raw_pid is not None else None
    except (TypeError, ValueError):
        host_pid = None
    if host_pid is not None and not _pid_alive(host_pid):
        return "stale", meta
    return "uncertain", meta


class _Suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


def _suppress() -> _Suppress:
    return _Suppress()


def _ensure_private_state_dir(state_dir: str) -> Path:
    base = Path(state_dir).resolve()
    if base.exists():
        try:
            mode = stat.S_IMODE(base.stat().st_mode)
        except OSError as exc:
            raise RuntimeError(f"E_SESSION_STATE_INVALID: cannot stat state directory: {exc}") from exc
        if os.name == "posix" and mode & 0o022:
            # Group/world writable: refuse to trust.
            raise RuntimeError("E_SESSION_STATE_INVALID: state directory must not be group/world writable (expected 0700)")
        with contextlib.suppress(OSError):
            os.chmod(base, 0o700)
    else:
        base.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(base, 0o700)
    return base


def _apply_winsize(fd: int, cols: int, rows: int) -> None:
    try:
        import fcntl
        import struct
        import termios
    except ImportError:
        return
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


_SENSITIVE_ARG_NAMES = frozenset(
    {
        "token",
        "api-key",
        "apikey",
        "api_key",
        "access-token",
        "access_token",
        "refresh-token",
        "refresh_token",
        "password",
        "passwd",
        "secret",
        "client-secret",
        "client_secret",
        "auth",
        "authorization",
    }
)


def _command_display(command: tuple[str, ...]) -> str:
    """Redacted one-line description safe for metadata (never raw secrets).

    Masks values following sensitive flags (``--token value``) in addition
    to the repository's standard inline credential patterns.
    """
    scrubbed: list[str] = []
    mask_next = False
    for part in command:
        if mask_next:
            scrubbed.append("<redacted>")
            mask_next = False
            continue
        name = part.lstrip("-").lower().replace("_", "-")
        if name in _SENSITIVE_ARG_NAMES or name.endswith(("token", "password", "secret")):
            mask_next = True
        scrubbed.append(part)
    return redact_sensitive(" ".join(scrubbed))


class SessionHost:
    """Owns a PTY child and serves the local authenticated control protocol."""

    def __init__(self, config: SessionHostConfig) -> None:
        if not config.command or any(not isinstance(c, str) or not c for c in config.command):
            raise ValueError("E_MANAGED_COMMAND_REQUIRED: The pty backend requires a non-empty agent command.")
        if not 1 <= int(config.cols) <= 1000 or not 1 <= int(config.rows) <= 1000:
            raise ValueError("invalid terminal size; expected 1..1000 for cols/rows")
        if int(config.replay_bytes) <= 0:
            raise ValueError("invalid replay capacity")
        self.config = config
        self.session_id = config.session_id or secrets.token_hex(16)
        self.token = secrets.token_urlsafe(32)
        self.started_at = now_iso()
        self.last_output_at: str | None = None
        self._replay = ReplayBuffer(capacity_bytes=int(config.replay_bytes))
        self._replay_lock = threading.Lock()
        self._master_fd: int | None = None
        self._root_pid: int | None = None
        self._exit_code: int | None = None
        # _exit_seen: waitpid() observed child death (authoritative liveness).
        self._exit_seen = threading.Event()
        # _pty_eof: reader stopped — EOF/EIO, drained dead child, or shutdown.
        self._pty_eof = threading.Event()
        # _child_exited: session fully finalized (drained + metadata written).
        self._child_exited = threading.Event()
        self._finalize_lock = threading.Lock()
        self._finalized = False
        self._shutdown = threading.Event()
        self._stream_clients: list[queue.Queue[bytes | None]] = []
        self._stream_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()

    # -- paths ---------------------------------------------------------
    def _paths(self) -> tuple[Path, Path, Path]:
        base = Path(self.config.state_dir).resolve()
        return (
            _resolved_inside(base, SESSION_METADATA_NAME),
            _resolved_inside(base, SESSION_TOKEN_NAME),
            _resolved_inside(base, SESSION_SOCKET_NAME),
        )

    def _write_metadata(self, state: str) -> None:
        meta_path, _, _ = self._paths()
        _atomic_json_write(
            meta_path,
            {
                "schema_version": SCHEMA_VERSION,
                "session_id": self.session_id,
                "backend": "pty",
                "host_pid": os.getpid(),
                "root_pid": self._root_pid,
                "executable": self.config.command[0] if self.config.command else "",
                "command_display": _command_display(self.config.command),
                "cwd": self.config.cwd,
                "started_at": self.started_at,
                "state": state,
                "exit_code": self._exit_code,
            },
        )

    def _write_token(self) -> None:
        _, token_path, _ = self._paths()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = token_path.with_name(f".{token_path.name}.{os.getpid()}.tmp")
        tmp.write_text(self.token + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, token_path)
        with contextlib.suppress(OSError):
            os.chmod(token_path, 0o600)

    def _remove_known_artifacts(self, base: Path) -> None:
        # Only the three files this host owns; never directories, never
        # anything outside the resolved state directory.
        for name in (SESSION_SOCKET_NAME, SESSION_METADATA_NAME, SESSION_TOKEN_NAME):
            try:
                target = _resolved_inside(base, name)
                if target.is_symlink() or target.is_file() or _is_socket_path(target):
                    target.unlink()
            except (OSError, ValueError):
                pass

    # -- lifecycle -----------------------------------------------------
    def run(self) -> int:
        if os.name != "posix":
            print("E_MANAGED_UNSUPPORTED_PLATFORM: Managed PTY backend requires a POSIX platform.", file=sys.stderr)
            return 2
        try:
            import pty  # noqa: F401
        except ImportError:
            print("E_MANAGED_UNSUPPORTED_PLATFORM: Managed PTY backend requires a POSIX platform.", file=sys.stderr)
            return 2
        try:
            base = _ensure_private_state_dir(self.config.state_dir)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        # Ownership decision before we own anything.
        decision, _meta = classify_startup_state(str(base))
        if decision == "live":
            print("E_SESSION_ALREADY_ACTIVE: A healthy managed session already exists for this state directory.", file=sys.stderr)
            return 2
        if decision == "uncertain":
            print(
                "E_SESSION_OWNERSHIP_UNCERTAIN: Managed session metadata exists but no authenticated live host is available "
                "while the recorded host process may still be alive. Refusing to start a second agent for this state directory.",
                file=sys.stderr,
            )
            return 2
        if decision == "stale":
            # Previous host provably dead: safe to clean only our artifacts.
            self._remove_known_artifacts(base)
        if not self.config.command:
            print("E_MANAGED_COMMAND_REQUIRED: The pty backend requires a non-empty agent command.", file=sys.stderr)
            return 2
        cwd = self.config.cwd or "."
        if not Path(cwd).is_dir():
            print(f"E_MANAGED_COMMAND_REQUIRED: working directory does not exist: {cwd}", file=sys.stderr)
            return 2

        self._write_token()
        pid, master = self._fork_child(cwd)
        self._root_pid = pid
        self._master_fd = master
        with contextlib.suppress(OSError, ValueError):
            os.set_blocking(master, False)
        _apply_winsize(master, int(self.config.cols), int(self.config.rows))

        # Bind control socket before announcing metadata.
        _, _, sock_path = self._paths()
        try:
            if sock_path.exists() or sock_path.is_symlink():
                with _suppress():
                    sock_path.unlink()
        except OSError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(sock_path))
            listener.listen(16)
        except OSError as exc:
            print(f"E_SESSION_START_TIMEOUT: cannot bind control socket: {exc}", file=sys.stderr)
            self._terminate_group()
            with contextlib.suppress(OSError):
                listener.close()
            return 2
        with contextlib.suppress(OSError):
            os.chmod(sock_path, 0o600)
        self._listener = listener
        self._write_metadata("running")

        reader = threading.Thread(target=self._pty_reader_loop, name="pty-reader", daemon=True)
        reader.start()
        reaper = threading.Thread(target=self._reaper_loop, name="child-reaper", daemon=True)
        reaper.start()
        try:
            self._accept_loop(listener)
        finally:
            self._finalize()
            self._flush_workers(FINAL_FLUSH_SECONDS)
        return 0

    def _finalize(self) -> None:
        """Record final state exactly once: wake streams, persist, close down.

        The lock is held for the whole body, so a second caller BLOCKS until
        the first finished instead of returning early. That matters because
        ``run()`` returns as soon as the accept loop sees ``_child_exited``:
        without the wait, the process (or the in-process host thread) could
        exit while the daemon thread that observed the exit was still between
        "stop the loops" and "write final metadata", losing the exit code and
        leaving ``state: running`` on disk forever.

        Final metadata is written BEFORE the control socket is removed, so the
        durable record always exists by the time clients can no longer connect.
        """
        with self._finalize_lock:
            if self._finalized:
                return
            self._child_exited.set()
            self._shutdown.set()
            with self._stream_lock:
                for watch in list(self._stream_clients):
                    with _suppress():
                        watch.put_nowait(None)
            try:
                state = "exited" if self._exit_seen.is_set() else "stopped"
                self._write_metadata(state)
            except (OSError, ValueError):
                pass
            if self._listener is not None:
                with _suppress():
                    self._listener.shutdown(socket.SHUT_RDWR)
                with _suppress():
                    self._listener.close()
            try:
                _, _, sock_path = self._paths()
            except ValueError:
                sock_path = None
            if sock_path is not None:
                with _suppress():
                    if sock_path.exists():
                        sock_path.unlink()
            self._close_master()
            self._finalized = True

    def _flush_workers(self, timeout: float) -> None:
        """Wait briefly for control-connection workers to send their last frame.

        Stream viewers learn the session ended from the ``exit`` event queued
        by ``_finalize``; without this bounded join the host process could exit
        first and the viewer would see a truncated frame instead.
        """
        deadline = time.monotonic() + timeout
        with self._workers_lock:
            workers = list(self._workers)
        current = threading.current_thread()
        for worker in workers:
            if worker is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)

    def _maybe_finalize(self) -> None:
        # Finalize once the child death was observed AND the PTY drained, or
        # once shutdown was requested (explicit terminate path).
        if self._shutdown.is_set() or (self._exit_seen.is_set() and self._pty_eof.is_set()):
            self._finalize()

    def _fork_child(self, cwd: str) -> tuple[int, int]:
        import pty

        pid, master = pty.fork()
        if pid == 0:
            # Child: new session so terminate can signal the whole group.
            with contextlib.suppress(OSError):
                os.setsid()
            try:
                os.chdir(cwd)
            except OSError:
                os._exit(127)
            cmd = list(self.config.command)
            try:
                os.execvpe(cmd[0], cmd, os.environ.copy())
            except OSError:
                os._exit(127)
            os._exit(127)  # pragma: no cover
        return pid, master

    def _close_master(self) -> None:
        if self._master_fd is not None:
            with _suppress():
                os.close(self._master_fd)
            self._master_fd = None

    # -- PTY reading ---------------------------------------------------
    def _pty_reader_loop(self) -> None:
        assert self._master_fd is not None
        sel = selectors.DefaultSelector()
        try:
            sel.register(self._master_fd, selectors.EVENT_READ)
        except (OSError, ValueError):
            self._pty_eof.set()
            self._maybe_finalize()
            return
        while not self._shutdown.is_set() and not self._child_exited.is_set():
            try:
                events = sel.select(timeout=0.2)
            except (OSError, ValueError):
                break
            if not events:
                # No readable bytes and child provably gone: PTY is drained.
                if self._root_pid is not None and (self._exit_seen.is_set() or not _pid_alive(self._root_pid)):
                    break
                continue
            try:
                chunk = os.read(self._master_fd, SNAPSHOT_CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not chunk:
                break
            with self._replay_lock:
                self._replay.append(chunk)
            self.last_output_at = now_iso()
            self._fanout(chunk)
        with _suppress():
            sel.close()
        # Every exit path leaves nothing readable: EOF/EIO (drained by
        # definition), a drained dead child, or a requested shutdown.
        self._pty_eof.set()
        self._maybe_finalize()

    def _reaper_loop(self) -> None:
        assert self._root_pid is not None
        pid = self._root_pid
        exit_code: int | None = None
        while True:
            try:
                done_pid, status = os.waitpid(pid, 0)
            except ChildProcessError:
                break
            except OSError:
                break
            if done_pid == pid:
                if os.WIFEXITED(status):
                    exit_code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    exit_code = 128 + os.WTERMSIG(status)
                else:
                    exit_code = 1
                break
        self._exit_code = exit_code if exit_code is not None else 1
        # Record death first so status() stops reporting alive, but keep the
        # session open until the reader drains final PTY output.
        self._exit_seen.set()
        self._maybe_finalize()

    def _fanout(self, chunk: bytes) -> None:
        """Deliver output to viewers, coalescing instead of dropping laggards.

        Line-by-line agent output arrives as many small chunks, so an item
        bounded queue fills long before the viewer is actually wedged. Dropping
        it there silently truncated the stream: the viewer lost the final bytes
        AND never received the ``exit`` event, surfacing as a closed connection
        mid-frame. A behind viewer now gets its backlog merged into a single
        newest-wins buffer capped at the replay budget, so memory stays bounded
        while the byte stream stays gap-free at the tail.
        """
        with self._stream_lock:
            clients = list(self._stream_clients)
        budget = max(1, int(self.config.replay_bytes))
        for watch in clients:
            try:
                watch.put_nowait(chunk)
                continue
            except queue.Full:
                pass
            merged = bytearray()
            ended = False
            while True:
                try:
                    pending = watch.get_nowait()
                except queue.Empty:
                    break
                if pending is None:
                    ended = True
                    break
                merged += pending
            merged += chunk
            if len(merged) > budget:
                del merged[: len(merged) - budget]
            with _suppress():
                watch.put_nowait(bytes(merged))
            if ended:
                # Preserve the end-of-session sentinel behind the backlog.
                with _suppress():
                    watch.put_nowait(None)

    # -- control plane -------------------------------------------------
    def _accept_loop(self, listener: socket.socket) -> None:
        listener.settimeout(0.3)
        while not self._shutdown.is_set() and not self._child_exited.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            worker = threading.Thread(target=self._handle_connection, args=(conn,), name="host-conn", daemon=True)
            worker.start()
            with self._workers_lock:
                # Drop finished workers so a long session cannot grow the list.
                self._workers = [thread for thread in self._workers if thread.is_alive()]
                self._workers.append(worker)

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(30.0)
            try:
                request = receive_message(conn)
            except SessionProtocolError:
                with _suppress():
                    conn.close()
                return
            if not isinstance(request, dict):
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
                return
            if request.get("version") != PROTOCOL_VERSION:
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
                return
            token = request.get("token")
            if not isinstance(token, str) or not secrets.compare_digest(token, self.token):
                self._send_error(conn, "E_SESSION_AUTH_FAILED: Managed session control authentication failed.")
                return
            op = request.get("op")
            if op == "status":
                self._handle_status(conn)
            elif op == "snapshot":
                self._handle_snapshot(conn, request)
            elif op == "send":
                self._handle_send(conn, request)
            elif op == "resize":
                self._handle_resize(conn, request)
            elif op == "stream":
                self._handle_stream(conn)
            elif op == "terminate":
                self._handle_terminate(conn)
            else:
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
        finally:
            # Stream handler closes its own connection.
            pass

    def _send_error(self, conn: socket.socket, message: str) -> None:
        with _suppress():
            send_message(conn, {"ok": False, "error": message})
        with _suppress():
            conn.close()

    def _is_live(self) -> bool:
        return self._root_pid is not None and not self._exit_seen.is_set() and _pid_alive(self._root_pid)

    def _handle_status(self, conn: socket.socket) -> None:
        try:
            send_message(
                conn,
                {
                    "ok": True,
                    "session_id": self.session_id,
                    "host_pid": os.getpid(),
                    "root_pid": self._root_pid,
                    "alive": self._is_live(),
                    "exit_code": self._exit_code,
                    "started_at": self.started_at,
                    "last_output_at": self.last_output_at,
                },
            )
        finally:
            with _suppress():
                conn.close()

    def _replay_snapshot(self, limit_bytes: int) -> bytes:
        with self._replay_lock:
            return self._replay.snapshot(limit_bytes)

    def _send_snapshot_frames(self, conn: socket.socket, data: bytes) -> None:
        send_message(conn, {"event": "snapshot_start", "total_bytes": len(data)})
        chunks = [data[offset : offset + SNAPSHOT_CHUNK_BYTES] for offset in range(0, len(data), SNAPSHOT_CHUNK_BYTES)]
        for seq, chunk in enumerate(chunks):
            send_message(conn, {"event": "snapshot_chunk", "seq": seq, "data_b64": base64.b64encode(chunk).decode("ascii")})
        send_message(conn, {"event": "snapshot_end", "total_bytes": len(data)})

    def _handle_snapshot(self, conn: socket.socket, request: dict[str, Any]) -> None:
        try:
            limit = request.get("limit_bytes", 512 * 1024)
            try:
                limit_int = int(limit) if limit is not None else 512 * 1024
            except (TypeError, ValueError):
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
                return
            limit_int = max(0, min(limit_int, 4 * 1024 * 1024))
            data = self._replay_snapshot(limit_int if limit_int > 0 else 0)
            if request.get("chunked") is True:
                with contextlib.suppress(OSError, SessionProtocolError):
                    self._send_snapshot_frames(conn, data)
                return
            try:
                send_message(conn, {"ok": True, "data_b64": base64.b64encode(data).decode("ascii")})
            except SessionProtocolError:
                self._send_error(
                    conn,
                    "E_SNAPSHOT_TOO_LARGE: snapshot exceeds the single-frame budget; retry with chunked snapshot.",
                )
        finally:
            with _suppress():
                conn.close()

    def _handle_send(self, conn: socket.socket, request: dict[str, Any]) -> None:
        try:
            raw_b64 = request.get("data_b64", "")
            if not isinstance(raw_b64, str):
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
                return
            try:
                payload = base64.b64decode(raw_b64.encode("ascii"), validate=True)
            except (binascii.Error, ValueError, UnicodeEncodeError):
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
                return
            if len(payload) > MAX_CONTROL_MESSAGE_BYTES:
                self._send_error(conn, f"E_SESSION_MESSAGE_TOO_LARGE: Managed session control message exceeds {MAX_CONTROL_MESSAGE_BYTES} bytes.")
                return
            if self._master_fd is None or self._child_exited.is_set():
                self._send_error(conn, "E_SESSION_STALE: Managed session metadata exists but no authenticated live host is available.")
                return
            try:
                # PTY master is non-blocking; retry briefly on EAGAIN.
                view = memoryview(payload)
                deadline = time.monotonic() + 5.0
                while view:
                    try:
                        written = os.write(self._master_fd, view)
                        view = view[written:]
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: PTY input buffer is full.")
                            return
                        time.sleep(0.01)
            except OSError as exc:
                self._send_error(conn, f"E_SESSION_STALE: cannot write to managed session: {exc}")
                return
            with _suppress():
                send_message(conn, {"ok": True})
        finally:
            with _suppress():
                conn.close()

    def _handle_resize(self, conn: socket.socket, request: dict[str, Any]) -> None:
        try:
            try:
                cols = int(request.get("cols", 0))
                rows = int(request.get("rows", 0))
            except (TypeError, ValueError):
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
                return
            if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
                return
            if self._master_fd is not None:
                _apply_winsize(self._master_fd, cols, rows)
            with _suppress():
                send_message(conn, {"ok": True})
        finally:
            with _suppress():
                conn.close()

    def _handle_stream(self, conn: socket.socket) -> None:
        stream_q: queue.Queue[bytes | None] = queue.Queue(maxsize=STREAM_QUEUE_MAX_ITEMS)
        with self._stream_lock:
            self._stream_clients.append(stream_q)
        try:
            conn.settimeout(None)
            initial = self._replay_snapshot(self.config.replay_bytes)
            try:
                self._send_snapshot_frames(conn, initial)
                if self._child_exited.is_set():
                    send_message(conn, {"event": "exit", "exit_code": self._exit_code})
                    return
            except (OSError, SessionProtocolError):
                return
            while True:
                try:
                    item = stream_q.get(timeout=60.0)
                except queue.Empty:
                    # Keep-alive tick; drop if peer gone.
                    try:
                        conn.sendall(b"")
                    except OSError:
                        break
                    if self._child_exited.is_set():
                        break
                    continue
                if item is None:
                    with contextlib.suppress(OSError, SessionProtocolError, ValueError):
                        send_message(conn, {"event": "exit", "exit_code": self._exit_code})
                    break
                raw_item = bytes(item)
                try:
                    # Keep every frame under the protocol budget.
                    for offset in range(0, len(raw_item), SNAPSHOT_CHUNK_BYTES):
                        piece = raw_item[offset : offset + SNAPSHOT_CHUNK_BYTES]
                        send_message(conn, {"event": "output", "data_b64": base64.b64encode(piece).decode("ascii")})
                except (OSError, SessionProtocolError):
                    break
        finally:
            with self._stream_lock:
                if stream_q in self._stream_clients:
                    self._stream_clients.remove(stream_q)
            with _suppress():
                conn.close()

    def _handle_terminate(self, conn: socket.socket) -> None:
        # Acknowledge FIRST so the client always observes a deterministic
        # response even though termination tears the host down.
        try:
            send_message(conn, {"ok": True, "accepted": True})
        except (OSError, SessionProtocolError):
            pass
        finally:
            with _suppress():
                conn.close()
        self._terminate_group()

    def _terminate_group(self) -> None:
        pid = self._root_pid
        if pid is None:
            self._exit_code = 1 if self._exit_code is None else self._exit_code
            self._exit_seen.set()
            self._shutdown.set()
            self._maybe_finalize()
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.05)
        if _pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        # Let the reaper record the exit code and the reader drain the final
        # output before shutdown forces finalization. Without this wait the
        # terminating thread can reach _finalize() first and persist
        # "stopped" with no exit code even though the child is already dead.
        self._exit_seen.wait(timeout=REAPER_HANDOFF_SECONDS)
        self._pty_eof.wait(timeout=REAPER_HANDOFF_SECONDS)
        # Unblock shutdown unconditionally so a wedged child cannot hang
        # termination.
        self._shutdown.set()
        self._maybe_finalize()


class _SuppressSend:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        # Swallow send errors inside stream setup; the stream loop will end.
        return exc[0] is not None and issubclass(exc[0], (OSError, SessionProtocolError, ValueError))


def _suppress_send() -> _SuppressSend:
    return _SuppressSend()


def _is_socket_path(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terminal_monitor.session_host", description="Detached managed PTY session host")
    parser.add_argument("--state-dir", required=True, help="Project-isolated monitor state directory")
    parser.add_argument("--cwd", default=".", help="Working directory for the agent command")
    parser.add_argument("--command-json", required=True, help="JSON array of command arguments")
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--rows", type=int, default=36)
    parser.add_argument("--replay-bytes", type=int, default=512 * 1024)
    parser.add_argument("--session-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        command = json.loads(args.command_json)
    except json.JSONDecodeError as exc:
        print(f"E_MANAGED_COMMAND_REQUIRED: invalid --command-json: {exc}", file=sys.stderr)
        return 2
    if not isinstance(command, list) or not command or any(not isinstance(c, str) or not c for c in command):
        print("E_MANAGED_COMMAND_REQUIRED: The pty backend requires a non-empty agent command.", file=sys.stderr)
        return 2
    config = SessionHostConfig(
        session_id=str(args.session_id or ""),
        command=tuple(command),
        cwd=str(args.cwd),
        state_dir=str(args.state_dir),
        cols=int(args.cols),
        rows=int(args.rows),
        replay_bytes=int(args.replay_bytes),
    )
    try:
        host = SessionHost(config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return host.run()


if __name__ == "__main__":
    raise SystemExit(main())
