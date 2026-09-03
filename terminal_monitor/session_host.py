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
from .session_protocol import MAX_CONTROL_MESSAGE_BYTES, PROTOCOL_VERSION, SessionProtocolError, receive_message, send_message
from .state import _atomic_json_write, now_iso

SESSION_METADATA_NAME = "managed-session.json"
SESSION_TOKEN_NAME = "session-token"
SESSION_SOCKET_NAME = "session-control.sock"
SCHEMA_VERSION = 1
STREAM_QUEUE_MAX_ITEMS = 64
TERMINATE_GRACE_SECONDS = 5.0


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
            with _suppress():
                client.close()
        if isinstance(resp, dict) and resp.get("ok"):
            return resp
        return None
    except (OSError, SessionProtocolError, ValueError, UnicodeDecodeError):
        return None


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
            raise RuntimeError(f"E_SESSION_START_TIMEOUT: cannot stat state directory: {exc}") from exc
        if os.name == "posix" and mode & 0o022:
            # Group/world writable: refuse to trust.
            raise RuntimeError("E_SESSION_START_TIMEOUT: state directory must not be group/world writable (expected 0700)")
        import contextlib as _ctx

        with _ctx.suppress(OSError):
            os.chmod(base, 0o700)
    else:
        base.mkdir(parents=True, exist_ok=True)
        import contextlib as _ctx2

        with _ctx2.suppress(OSError):
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
        self._child_exited = threading.Event()
        self._shutdown = threading.Event()
        self._stream_clients: list[queue.Queue[bytes | None]] = []
        self._stream_lock = threading.Lock()
        self._listener: socket.socket | None = None

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
                "command": list(self.config.command),
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
        # Duplicate / stale handling before we own anything.
        meta_path = _resolved_inside(base, SESSION_METADATA_NAME)
        if meta_path.is_file():
            live = _try_authenticated_status(str(base))
            if live and live.get("session_id"):
                print("E_SESSION_ALREADY_ACTIVE: A healthy managed session already exists for this state directory.", file=sys.stderr)
                return 2
            # Stale: remove only known session artifacts inside the state dir.
            for name in (SESSION_SOCKET_NAME, SESSION_METADATA_NAME, SESSION_TOKEN_NAME):
                try:
                    p = _resolved_inside(base, name)
                    if p.is_symlink() or p.is_file() or _is_socket_path(p):
                        p.unlink()
                except (OSError, ValueError):
                    pass
            # If host PID from stale metadata is still alive but unauthenticated,
            # do not kill it; we simply take over the state dir slot.
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
            self._shutdown.set()
            with _suppress():
                listener.close()
            with _suppress():
                if sock_path.exists():
                    sock_path.unlink()
            # Record final state; keep metadata with exit code for reconnectors.
            try:
                state = "exited" if self._child_exited.is_set() else "stopped"
                self._write_metadata(state)
            except OSError:
                pass
            self._close_master()
        return 0

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
            self._child_exited.set()
            return
        while not self._shutdown.is_set() and not self._child_exited.is_set():
            try:
                events = sel.select(timeout=0.2)
            except (OSError, ValueError):
                break
            if not events:
                # Poll child liveness so EOF without readable bytes still ends.
                if self._root_pid is not None and not _pid_alive(self._root_pid):
                    break
                continue
            try:
                chunk = os.read(self._master_fd, 65536)
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
        # Do not set _child_exited here; reaper owns exit bookkeeping.

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
        self._child_exited.set()
        self._shutdown.set()
        # Wake stream consumers with an exit sentinel.
        with self._stream_lock:
            for q in list(self._stream_clients):
                with _suppress():
                    q.put_nowait(None)
        # Close listener so accept loop ends promptly.
        if self._listener is not None:
            with _suppress():
                self._listener.shutdown(socket.SHUT_RDWR)
            with _suppress():
                self._listener.close()
        with contextlib.suppress(OSError):
            self._write_metadata("exited")

    def _fanout(self, chunk: bytes) -> None:
        dead: list[queue.Queue[bytes | None]] = []
        with self._stream_lock:
            clients = list(self._stream_clients)
        for q in clients:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                dead.append(q)
        if dead:
            with self._stream_lock:
                for q in dead:
                    if q in self._stream_clients:
                        self._stream_clients.remove(q)
                        with _suppress():
                            q.put_nowait(None)

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

    def _handle_status(self, conn: socket.socket) -> None:
        try:
            alive = self._root_pid is not None and not self._child_exited.is_set() and _pid_alive(self._root_pid)
            send_message(
                conn,
                {
                    "ok": True,
                    "session_id": self.session_id,
                    "host_pid": os.getpid(),
                    "root_pid": self._root_pid,
                    "alive": bool(alive),
                    "exit_code": self._exit_code,
                    "started_at": self.started_at,
                    "last_output_at": self.last_output_at,
                },
            )
        finally:
            with _suppress():
                conn.close()

    def _handle_snapshot(self, conn: socket.socket, request: dict[str, Any]) -> None:
        try:
            limit = request.get("limit_bytes", 512 * 1024)
            try:
                limit_int = int(limit) if limit is not None else 512 * 1024
            except (TypeError, ValueError):
                self._send_error(conn, "E_SESSION_PROTOCOL_INVALID: Managed session control message is invalid.")
                return
            limit_int = max(0, min(limit_int, 4 * 1024 * 1024))
            with self._replay_lock:
                data = self._replay.snapshot(limit_int if limit_int > 0 else 0)
            send_message(conn, {"ok": True, "data_b64": base64.b64encode(data).decode("ascii")})
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
            with self._replay_lock:
                initial = self._replay.snapshot()
            with _suppress_send():
                send_message(conn, {"event": "snapshot", "data_b64": base64.b64encode(initial).decode("ascii")})
                if self._child_exited.is_set():
                    send_message(conn, {"event": "exit", "exit_code": self._exit_code})
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
                    with _suppress_send():
                        send_message(conn, {"event": "exit", "exit_code": self._exit_code})
                    break
                try:
                    send_message(conn, {"event": "output", "data_b64": base64.b64encode(bytes(item)).decode("ascii")})
                except (OSError, SessionProtocolError):
                    break
        finally:
            with self._stream_lock:
                if stream_q in self._stream_clients:
                    self._stream_clients.remove(stream_q)
            with _suppress():
                conn.close()

    def _handle_terminate(self, conn: socket.socket) -> None:
        try:
            self._terminate_group()
            with _suppress():
                send_message(conn, {"ok": True})
        finally:
            with _suppress():
                conn.close()

    def _terminate_group(self) -> None:
        pid = self._root_pid
        if pid is None:
            self._child_exited.set()
            self._shutdown.set()
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
