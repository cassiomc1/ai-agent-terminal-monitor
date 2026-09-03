"""Managed PTY backend: client for the detached SessionHost."""
from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import BaseTerminalBackend, TerminalIdentity
from .safety import SPECIAL_KEY_CODES
from .session_protocol import (
    MAX_SEND_BYTES,
    PROTOCOL_VERSION,
    SEND_CHUNK_BYTES,
    SNAPSHOT_CHUNK_BYTES,
    FramedReader,
    SessionProtocolError,
    receive_message,
    send_message,
)
from .types import TabResult

SESSION_METADATA_NAME = "managed-session.json"
SESSION_TOKEN_NAME = "session-token"
SESSION_SOCKET_NAME = "session-control.sock"
SESSION_LOCK_NAME = "managed-session.lock"
STARTUP_TIMEOUT_SECONDS = 10.0
STARTUP_LOCK_TIMEOUT_SECONDS = 30.0
# Upper bound for waiting on a spawned host to exit. Must exceed the host-side
# SIGTERM grace so a cooperating host is always reaped instead of orphaned.
TERMINATE_REAP_SECONDS = 15.0

# Spawned SessionHost processes keyed by resolved state dir. The starter
# client owns the Popen handle so test and monitor teardowns can reap the
# host process instead of leaking it (and tripping ResourceWarning).
_HOST_PROCS: dict[str, subprocess.Popen[bytes]] = {}
_HOST_PROCS_LOCK = threading.Lock()


def _register_host_proc(state_dir: str, proc: subprocess.Popen[bytes]) -> None:
    with _HOST_PROCS_LOCK:
        _HOST_PROCS[str(Path(state_dir).resolve())] = proc


def _reap_host_proc(state_dir: str, timeout: float) -> bool | None:
    """Poll (timeout=0) or wait for the spawned host, dropping reaped handles.

    Returns True once the spawned host has exited, False while it is still
    running, and None when this process never spawned a host for ``state_dir``.
    """
    key = str(Path(state_dir).resolve())
    with _HOST_PROCS_LOCK:
        proc = _HOST_PROCS.get(key)
    if proc is None:
        return None
    try:
        if timeout > 0:
            proc.wait(timeout=timeout)
        else:
            proc.poll()
    except (OSError, ValueError, subprocess.SubprocessError):
        return proc.returncode is not None
    if proc.returncode is not None:
        with _HOST_PROCS_LOCK:
            _HOST_PROCS.pop(key, None)
        return True
    return False


@contextlib.contextmanager
def _startup_lock(state_dir: str, timeout: float = STARTUP_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialize managed-session startup decisions per state directory.

    A POSIX advisory lock held only across startup/adoption coordination:
    re-check live ownership, optionally spawn exactly one host, wait for
    authenticated readiness, then release. Never held during supervision,
    so monitor restarts are unaffected.
    """
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("E_MANAGED_UNSUPPORTED_PLATFORM: Managed PTY backend requires a POSIX platform.") from exc
    base = Path(state_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / SESSION_LOCK_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("E_SESSION_START_TIMEOUT: timed out waiting for the managed-session startup lock.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)

_MANAGED_KEY_BYTES: dict[str, bytes] = {
    "enter": b"\n",
    "return": b"\n",
    "\r": b"\r",
    "\n": b"\n",
    "tab": b"\t",
    "\t": b"\t",
    "esc": b"\x1b",
    "escape": b"\x1b",
    "\x1b": b"\x1b",
    "ctrl+c": b"\x03",
    "ctrl_c": b"\x03",
    "\x03": b"\x03",
    "ctrl+d": b"\x04",
    "ctrl_d": b"\x04",
    "\x04": b"\x04",
    "ctrl+p": b"\x10",
    "ctrl_p": b"\x10",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "left": b"\x1b[D",
    "right": b"\x1b[C",
    "space": b" ",
    "backspace": b"\x7f",
}


def _state_paths(state_dir: str) -> tuple[Path, Path, Path]:
    base = Path(state_dir).resolve()
    return (base / SESSION_METADATA_NAME, base / SESSION_TOKEN_NAME, base / SESSION_SOCKET_NAME)


def _read_metadata(state_dir: str) -> dict[str, Any] | None:
    meta_path, _, _ = _state_paths(state_dir)
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def managed_session_is_reconnectable(state_dir: str) -> bool:
    meta = _read_metadata(state_dir)
    if not meta or meta.get("backend") != "pty":
        return False
    _, token_path, socket_path = _state_paths(state_dir)
    if not token_path.is_file() or not socket_path.exists():
        return False
    try:
        client = ManagedSessionClient.connect(state_dir)
    except (OSError, ValueError, SessionProtocolError):
        return False
    try:
        status = client.status()
    except (OSError, ValueError, SessionProtocolError):
        return False
    finally:
        with _suppress():
            client.close()
    return bool(status.alive)


class _Suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return True


def _suppress() -> _Suppress:
    return _Suppress()


def _read_chunked_snapshot(reader: FramedReader) -> bytes:
    """Assemble a snapshot_start/chunk/end sequence, failing closed on defects.

    Validates exact byte ordering (monotonic seq), exact total (sum matches
    the announced total_bytes), and rejects any malformed or partial
    sequence instead of yielding corrupted history.
    """
    try:
        first = reader.read_message()
    except SessionProtocolError as exc:
        raise SessionProtocolError(f"E_SESSION_PROTOCOL_INVALID: incomplete snapshot sequence: {exc}") from exc
    if first.get("event") != "snapshot_start" or not isinstance(first.get("total_bytes"), int):
        raise SessionProtocolError("E_SESSION_PROTOCOL_INVALID: malformed snapshot sequence")
    total = int(first["total_bytes"])
    if total < 0 or total > 8 * 1024 * 1024:
        raise SessionProtocolError("E_SESSION_PROTOCOL_INVALID: malformed snapshot sequence")
    parts: list[bytes] = []
    received = 0
    expected_seq = 0
    while True:
        try:
            event = reader.read_message()
        except SessionProtocolError as exc:
            raise SessionProtocolError(f"E_SESSION_PROTOCOL_INVALID: incomplete snapshot sequence: {exc}") from exc
        kind = event.get("event")
        if kind == "snapshot_chunk":
            if event.get("seq") != expected_seq or not isinstance(event.get("data_b64"), str):
                raise SessionProtocolError("E_SESSION_PROTOCOL_INVALID: malformed snapshot sequence")
            try:
                chunk = base64.b64decode(event["data_b64"].encode("ascii"), validate=True)
            except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
                raise SessionProtocolError(f"E_SESSION_PROTOCOL_INVALID: malformed snapshot sequence: {exc}") from exc
            if len(chunk) > SNAPSHOT_CHUNK_BYTES + 1024:
                raise SessionProtocolError("E_SESSION_PROTOCOL_INVALID: malformed snapshot sequence")
            received += len(chunk)
            if received > total:
                raise SessionProtocolError("E_SESSION_PROTOCOL_INVALID: malformed snapshot sequence")
            parts.append(chunk)
            expected_seq += 1
        elif kind == "snapshot_end":
            if event.get("total_bytes") != total or received != total:
                raise SessionProtocolError("E_SESSION_PROTOCOL_INVALID: malformed snapshot sequence")
            return b"".join(parts)
        else:
            raise SessionProtocolError("E_SESSION_PROTOCOL_INVALID: malformed snapshot sequence")


@dataclass(frozen=True)
class ManagedSessionStatus:
    session_id: str
    host_pid: int
    root_pid: int
    alive: bool
    exit_code: int | None
    started_at: str
    last_output_at: str | None


class ManagedSessionClient:
    """Authenticated control client for one detached SessionHost."""

    def __init__(self, state_dir: str, token: str, session_id: str = "") -> None:
        self._state_dir = str(Path(state_dir).resolve())
        self._token = token
        self._session_id = session_id
        self._closed = False

    @classmethod
    def _load_token(cls, state_dir: str) -> str:
        _, token_path, _ = _state_paths(state_dir)
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise FileNotFoundError(f"E_SESSION_NOT_FOUND: No active managed session exists for this state directory: {exc}") from exc
        if not token:
            raise ValueError("E_SESSION_AUTH_FAILED: Managed session control authentication failed.")
        return token

    @classmethod
    def connect(cls, state_dir: str) -> ManagedSessionClient:
        token = cls._load_token(state_dir)
        meta = _read_metadata(state_dir)
        session_id = str((meta or {}).get("session_id", ""))
        client = cls(state_dir, token, session_id)
        # Validate immediately so stale metadata fails fast.
        status = client.status()
        if session_id and status.session_id and status.session_id != session_id:
            client.close()
            raise ValueError("E_SESSION_STALE: Managed session metadata exists but no authenticated live host is available.")
        return client

    @classmethod
    def start(cls, *, state_dir: str, command: tuple[str, ...], cwd: str) -> ManagedSessionClient:
        if not command or any(not isinstance(c, str) or not c for c in command):
            raise ValueError("E_MANAGED_COMMAND_REQUIRED: The pty backend requires a non-empty agent command.")
        if os.name != "posix":
            raise RuntimeError("E_MANAGED_UNSUPPORTED_PLATFORM: Managed PTY backend requires a POSIX platform.")
        base = Path(state_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        try:
            import stat as _stat

            mode = _stat.S_IMODE(base.stat().st_mode)
            if mode & 0o022:
                raise RuntimeError("E_SESSION_STATE_INVALID: state directory must not be group/world writable (expected 0700)")
            os.chmod(base, 0o700)
        except OSError:
            pass
        with _startup_lock(str(base)):
            return cls._start_locked(state_dir=str(base), command=tuple(command), cwd=str(cwd or "."))

    @classmethod
    def _start_locked(cls, *, state_dir: str, command: tuple[str, ...], cwd: str) -> ManagedSessionClient:
        from .session_host import classify_startup_state

        # Re-check under the lock: a concurrent starter may have won.
        decision, _meta = classify_startup_state(state_dir)
        if decision == "live":
            return cls.connect(state_dir)
        if decision == "uncertain":
            raise RuntimeError(
                "E_SESSION_OWNERSHIP_UNCERTAIN: Managed session metadata exists but no authenticated live host is available "
                "while the recorded host process may still be alive. Refusing to start a second agent for this state directory."
            )
        # Stale or absent: remove a dead socket file eagerly so bind succeeds.
        _, _, socket_path = _state_paths(state_dir)
        if (socket_path.is_symlink() or socket_path.exists()) and not _try_status(state_dir):
            # Only unlink if nothing answers; never delete directories.
            with _suppress():
                socket_path.unlink()
        argv = [
            sys.executable,
            "-m",
            "terminal_monitor.session_host",
            "--state-dir",
            state_dir,
            "--cwd",
            cwd,
            "--command-json",
            json.dumps(list(command)),
        ]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise RuntimeError(f"E_SESSION_START_TIMEOUT: Managed SessionHost did not become ready: {exc}") from exc
        _register_host_proc(state_dir, proc)
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                client = cls.connect(state_dir)
            except (OSError, ValueError, SessionProtocolError) as exc:
                last_error = exc
                time.sleep(0.1)
                continue
            try:
                status = client.status()
            except (OSError, ValueError, SessionProtocolError) as exc:
                last_error = exc
                with _suppress():
                    client.close()
                time.sleep(0.1)
                continue
            if status.alive:
                return client
            last_error = RuntimeError("E_SESSION_STALE: Managed session exited during startup.")
            with _suppress():
                client.close()
            break
        raise RuntimeError(f"E_SESSION_START_TIMEOUT: Managed SessionHost did not become ready before the startup deadline.{f' ({last_error})' if last_error else ''}")

    def _request(self, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        if self._closed:
            raise ValueError("E_SESSION_STALE: client is closed")
        _, _, socket_path = _state_paths(self._state_dir)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(socket_path))
            body = dict(payload)
            body.setdefault("version", PROTOCOL_VERSION)
            body.setdefault("token", self._token)
            send_message(sock, body)
            return receive_message(sock)
        except OSError as exc:
            raise ConnectionError(f"E_SESSION_STALE: cannot reach managed session host: {exc}") from exc
        finally:
            with _suppress():
                sock.close()

    def _checked(self, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        return self._raise_for_response(self._request(payload, timeout=timeout))

    @staticmethod
    def _raise_for_response(resp: Any) -> dict[str, Any]:
        if not isinstance(resp, dict) or not resp.get("ok"):
            error = str(resp.get("error", "unknown error")) if isinstance(resp, dict) else "invalid response"
            if "E_SESSION_AUTH_FAILED" in error:
                raise PermissionError(error)
            if "E_SESSION_MESSAGE_TOO_LARGE" in error:
                raise ValueError(error)
            if "E_SESSION_STALE" in error:
                raise ConnectionError(error)
            raise RuntimeError(error)
        return resp

    def status(self) -> ManagedSessionStatus:
        try:
            resp = self._request({"op": "status"})
        except (ConnectionError, OSError, SessionProtocolError, ValueError) as exc:
            # Host may have exited cleanly and removed the socket; fall back
            # to durable metadata so child exit remains observable.
            fallback = _read_metadata(self._state_dir)
            if isinstance(fallback, dict) and (fallback.get("state") == "exited" or fallback.get("exit_code") is not None):
                try:
                    exit_code = fallback.get("exit_code")
                    return ManagedSessionStatus(
                        session_id=str(fallback.get("session_id", self._session_id)),
                        host_pid=int(fallback.get("host_pid", 0) or 0),
                        root_pid=int(fallback.get("root_pid", 0) or 0),
                        alive=False,
                        exit_code=int(exit_code) if exit_code is not None else 1,
                        started_at=str(fallback.get("started_at", "")),
                        last_output_at=None,
                    )
                except (TypeError, ValueError):
                    pass
            if isinstance(exc, (ConnectionError, OSError)):
                raise ConnectionError(f"E_SESSION_STALE: cannot reach managed session host: {exc}") from exc
            raise
        if not resp.get("ok"):
            raise RuntimeError(str(resp.get("error", "status failed")))
        try:
            return ManagedSessionStatus(
                session_id=str(resp.get("session_id", self._session_id)),
                host_pid=int(resp.get("host_pid", 0)),
                root_pid=int(resp.get("root_pid", 0)),
                alive=bool(resp.get("alive", False)),
                exit_code=int(resp["exit_code"]) if resp.get("exit_code") is not None else None,
                started_at=str(resp.get("started_at", "")),
                last_output_at=str(resp["last_output_at"]) if resp.get("last_output_at") is not None else None,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"E_SESSION_PROTOCOL_INVALID: invalid status response: {exc}") from exc

    def snapshot(self, limit_bytes: int = 512 * 1024) -> bytes:
        """Fetch replay via chunked transfer; each frame stays under 64 KiB."""
        _, _, socket_path = _state_paths(self._state_dir)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30.0)
        try:
            sock.connect(str(socket_path))
            body = {"version": PROTOCOL_VERSION, "token": self._token, "op": "snapshot", "limit_bytes": int(limit_bytes), "chunked": True}
            send_message(sock, body)
            return _read_chunked_snapshot(FramedReader(sock))
        except OSError as exc:
            raise ConnectionError(f"E_SESSION_STALE: cannot reach managed session host: {exc}") from exc
        finally:
            with _suppress():
                sock.close()

    def send_bytes(self, payload: bytes) -> None:
        """Write raw bytes to the managed PTY, chunking past one frame's capacity.

        Base64 expands the payload by 4/3, so a single frame cannot carry the
        full decoded bound. Payloads that still fit keep using the one-shot
        ``send`` op; larger ones stream as ``send_start``/``send_chunk``/
        ``send_end`` on one authenticated connection. The host stages and
        validates the whole transfer before writing anything to the PTY.
        """
        data = bytes(payload)
        if len(data) > MAX_SEND_BYTES:
            raise ValueError(f"E_SESSION_MESSAGE_TOO_LARGE: Managed session input exceeds {MAX_SEND_BYTES} bytes.")
        if len(data) <= SEND_CHUNK_BYTES:
            self._checked({"op": "send", "data_b64": base64.b64encode(data).decode("ascii")})
            return
        self._send_chunked(data)

    def _send_chunked(self, data: bytes) -> None:
        if self._closed:
            raise ValueError("E_SESSION_STALE: client is closed")
        _, _, socket_path = _state_paths(self._state_dir)
        transfer_id = secrets.token_hex(8)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30.0)
        try:
            try:
                sock.connect(str(socket_path))
                send_message(
                    sock,
                    {
                        "version": PROTOCOL_VERSION,
                        "token": self._token,
                        "op": "send_start",
                        "transfer_id": transfer_id,
                        "total_bytes": len(data),
                    },
                )
                for seq, offset in enumerate(range(0, len(data), SEND_CHUNK_BYTES)):
                    piece = data[offset : offset + SEND_CHUNK_BYTES]
                    send_message(
                        sock,
                        {
                            "op": "send_chunk",
                            "transfer_id": transfer_id,
                            "seq": seq,
                            "data_b64": base64.b64encode(piece).decode("ascii"),
                        },
                    )
                send_message(sock, {"op": "send_end", "transfer_id": transfer_id, "total_bytes": len(data)})
            except SessionProtocolError as exc:
                # The host rejected the transfer and closed mid-stream; prefer
                # its reason (it may still be buffered) over the write failure.
                pending: Any = None
                with contextlib.suppress(OSError, SessionProtocolError, ValueError):
                    pending = receive_message(sock)
                if isinstance(pending, dict):
                    self._raise_for_response(pending)
                raise ConnectionError(f"E_SESSION_STALE: cannot reach managed session host: {exc}") from exc
            resp = receive_message(sock)
        except OSError as exc:
            raise ConnectionError(f"E_SESSION_STALE: cannot reach managed session host: {exc}") from exc
        finally:
            with _suppress():
                sock.close()
        self._raise_for_response(resp)

    def resize(self, cols: int, rows: int) -> None:
        if not isinstance(cols, int) or not isinstance(rows, int) or not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
            raise ValueError("E_SESSION_PROTOCOL_INVALID: invalid terminal size; expected 1..1000")
        self._checked({"op": "resize", "cols": cols, "rows": rows})

    def terminate_session(self) -> None:
        """Terminate the managed session; already-exited sessions succeed silently."""
        try:
            self._checked({"op": "terminate"}, timeout=TERMINATE_REAP_SECONDS)
        except (ConnectionError, OSError, SessionProtocolError) as failure:
            # The host is unreachable: either gone, or torn down mid-frame
            # (a connection left in the backlog of a closing listener reads as
            # a truncated message). A host this process spawned proves the
            # outcome by exiting; otherwise durable metadata must show that the
            # session finished. Anything else is reported as a failure, without
            # waiting on a host that was never asked to stop.
            if _reap_host_proc(self._state_dir, timeout=TERMINATE_REAP_SECONDS) is True:
                return
            try:
                final = self.status()
            except (OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError):
                raise failure from None
            if final.alive:
                raise failure from None
            return
        _reap_host_proc(self._state_dir, timeout=TERMINATE_REAP_SECONDS)

    def stream(self) -> Iterator[bytes]:
        """Yield replay snapshot then live output until the child exits."""
        _, _, socket_path = _state_paths(self._state_dir)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        try:
            sock.connect(str(socket_path))
            send_message(sock, {"version": PROTOCOL_VERSION, "token": self._token, "op": "stream"})
            sock.settimeout(70.0)
            reader = FramedReader(sock)
            try:
                initial = _read_chunked_snapshot(reader)
            except SessionProtocolError:
                return
            if initial:
                yield initial
            while True:
                try:
                    event = reader.read_message()
                except SessionProtocolError:
                    break
                kind = event.get("event")
                if kind == "output":
                    raw = event.get("data_b64", "")
                    if not isinstance(raw, str) or not raw:
                        break
                    try:
                        yield base64.b64decode(raw.encode("ascii"))
                    except (binascii.Error, ValueError):
                        break
                elif kind == "exit":
                    break
                else:
                    break
        except OSError:
            return
        finally:
            with _suppress():
                sock.close()

    def close(self) -> None:
        self._closed = True
        _reap_host_proc(self._state_dir, timeout=0)


def _try_status(state_dir: str) -> dict[str, Any] | None:
    try:
        client = ManagedSessionClient.connect(state_dir)
    except (OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError):
        return None
    try:
        status = client.status()
    except (OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError):
        return None
    finally:
        with _suppress():
            client.close()
    return {"session_id": status.session_id, "alive": status.alive}


def _map_key_to_bytes(key: str) -> bytes:
    normalized = key.lower().strip()
    if normalized in _MANAGED_KEY_BYTES:
        return _MANAGED_KEY_BYTES[normalized]
    code = SPECIAL_KEY_CODES.get(normalized)
    if code is not None:
        try:
            return bytes([int(code) % 256])
        except (TypeError, ValueError):
            pass
    if len(key) == 1:
        return key.encode("utf-8", "replace")
    # Unknown multi-char key names: send nothing but report failure upstream.
    raise ValueError(f"unsupported key: {key!r}")


class ManagedPTYBackend(BaseTerminalBackend):
    """Monitor-facing client for a detached managed PTY session."""

    def __init__(self, state_dir: str | None = None) -> None:
        self._state_dir = state_dir
        self._client: ManagedSessionClient | None = None
        self._session_id = ""

    def name(self) -> str:
        return "pty"

    @property
    def owns_process(self) -> bool:
        return True

    def _resolve_state_dir(self, state_dir: str | None = None) -> str:
        if state_dir:
            return str(Path(state_dir).resolve())
        if self._state_dir:
            return str(Path(self._state_dir).resolve())
        raise ValueError("E_SESSION_NOT_FOUND: No active managed session exists for this state directory.")

    def _ensure_client(self, state_dir: str) -> ManagedSessionClient:
        if self._client is None:
            self._client = ManagedSessionClient.connect(state_dir)
            import contextlib as _ctx

            with _ctx.suppress(OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError):
                self._session_id = self._client.status().session_id
        return self._client

    def start_managed(self, command: tuple[str, ...], *, cwd: str, state_dir: str) -> TerminalIdentity:
        client = ManagedSessionClient.start(state_dir=str(state_dir), command=tuple(command), cwd=str(cwd))
        self._client = client
        self._state_dir = str(Path(state_dir).resolve())
        try:
            status = client.status()
            self._session_id = status.session_id
            return TerminalIdentity(project_path=str(cwd), session_id=status.session_id, title=status.session_id, root_pid=status.root_pid)
        except (OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError) as exc:
            raise RuntimeError(f"E_SESSION_STALE: cannot read managed session status: {exc}") from exc

    def reconnect(self, *, state_dir: str) -> TerminalIdentity:
        client = ManagedSessionClient.connect(str(state_dir))
        self._client = client
        self._state_dir = str(Path(state_dir).resolve())
        status = client.status()
        self._session_id = status.session_id
        meta = _read_metadata(str(state_dir))
        project_path = str((meta or {}).get("cwd", ""))
        return TerminalIdentity(project_path=project_path, session_id=status.session_id, title=status.session_id, root_pid=status.root_pid)

    def get_tab_for_identity(self, process: str, identity: TerminalIdentity) -> TabResult:
        del process, identity
        return self.get_tab("", None)

    def get_tab(self, process: str, title: str | None = None) -> TabResult:
        del process, title
        try:
            state_dir = self._resolve_state_dir()
            client = self._ensure_client(state_dir)
            raw = client.snapshot()
            status = client.status()
        except (OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}
        text = raw.decode("utf-8", errors="replace")
        session = status.session_id or self._session_id
        return {
            "ok": True,
            "error": "",
            "win": session,
            "tab": session,
            "title": session,
            "busy": bool(status.alive),
            "wname": session,
            "hist": text,
        }

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        del process, title
        try:
            state_dir = self._resolve_state_dir()
            client = self._ensure_client(state_dir)
            # Append Enter exactly once.
            text = payload.rstrip("\r\n") + "\n" if payload else "\n"
            client.send_bytes(text.encode("utf-8", "replace"))
            return True, "SENT"
        except (OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError) as exc:
            return False, str(exc)

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        del process, title
        try:
            data = _map_key_to_bytes(key)
        except ValueError as exc:
            return False, str(exc)
        try:
            state_dir = self._resolve_state_dir()
            client = self._ensure_client(state_dir)
            client.send_bytes(data)
            return True, "SENT"
        except (OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError) as exc:
            return False, str(exc)

    def get_pids(self, process: str) -> list[int]:
        del process
        try:
            state_dir = self._resolve_state_dir()
            client = self._ensure_client(state_dir)
            status = client.status()
        except (OSError, ValueError, SessionProtocolError, PermissionError, ConnectionError, RuntimeError):
            return []
        if status.alive and status.root_pid:
            return [int(status.root_pid)]
        return []

    def close(self) -> None:
        if self._client is not None:
            with _suppress():
                self._client.close()
            self._client = None
