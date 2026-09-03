"""Managed PTY backend: client for the detached SessionHost."""
from __future__ import annotations

import base64
import binascii
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import BaseTerminalBackend, TerminalIdentity
from .safety import SPECIAL_KEY_CODES
from .session_protocol import MAX_CONTROL_MESSAGE_BYTES, PROTOCOL_VERSION, SessionProtocolError, receive_message, send_message
from .types import TabResult

SESSION_METADATA_NAME = "managed-session.json"
SESSION_TOKEN_NAME = "session-token"
SESSION_SOCKET_NAME = "session-control.sock"
STARTUP_TIMEOUT_SECONDS = 10.0

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
                raise RuntimeError("E_SESSION_START_TIMEOUT: state directory must not be group/world writable (expected 0700)")
            os.chmod(base, 0o700)
        except OSError:
            pass
        # Adopt a healthy host instead of double-starting.
        if managed_session_is_reconnectable(str(base)):
            return cls.connect(str(base))
        # If stale artifacts exist without a live host, the host itself cleans
        # them; remove a dead socket file eagerly so bind succeeds.
        _, _, socket_path = _state_paths(str(base))
        if (socket_path.is_symlink() or socket_path.exists()) and not _try_status(str(base)):
            # Only unlink if nothing answers; never delete directories.
            with _suppress():
                socket_path.unlink()
        argv = [
            sys.executable,
            "-m",
            "terminal_monitor.session_host",
            "--state-dir",
            str(base),
            "--cwd",
            str(cwd or "."),
            "--command-json",
            json.dumps(list(command)),
        ]
        try:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise RuntimeError(f"E_SESSION_START_TIMEOUT: Managed SessionHost did not become ready: {exc}") from exc
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                client = cls.connect(str(base))
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
        resp = self._request(payload, timeout=timeout)
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
        resp = self._checked({"op": "snapshot", "limit_bytes": int(limit_bytes)})
        raw = resp.get("data_b64", "")
        if not isinstance(raw, str):
            raise RuntimeError("E_SESSION_PROTOCOL_INVALID: invalid snapshot response")
        try:
            return base64.b64decode(raw.encode("ascii"))
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(f"E_SESSION_PROTOCOL_INVALID: invalid snapshot payload: {exc}") from exc

    def send_bytes(self, payload: bytes) -> None:
        if len(payload) > MAX_CONTROL_MESSAGE_BYTES:
            raise ValueError(f"E_SESSION_MESSAGE_TOO_LARGE: Managed session control message exceeds {MAX_CONTROL_MESSAGE_BYTES} bytes.")
        self._checked({"op": "send", "data_b64": base64.b64encode(bytes(payload)).decode("ascii")})

    def resize(self, cols: int, rows: int) -> None:
        if not isinstance(cols, int) or not isinstance(rows, int) or not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
            raise ValueError("E_SESSION_PROTOCOL_INVALID: invalid terminal size; expected 1..1000")
        self._checked({"op": "resize", "cols": cols, "rows": rows})

    def terminate_session(self) -> None:
        self._checked({"op": "terminate"}, timeout=15.0)

    def stream(self) -> Iterator[bytes]:
        """Yield replay snapshot then live output until the child exits."""
        _, _, socket_path = _state_paths(self._state_dir)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        try:
            sock.connect(str(socket_path))
            send_message(sock, {"version": PROTOCOL_VERSION, "token": self._token, "op": "stream"})
            sock.settimeout(70.0)
            while True:
                try:
                    event = receive_message(sock)
                except SessionProtocolError:
                    break
                kind = event.get("event")
                if kind in ("snapshot", "output"):
                    raw = event.get("data_b64", "")
                    if isinstance(raw, str) and raw:
                        try:
                            yield base64.b64decode(raw.encode("ascii"))
                        except (binascii.Error, ValueError):
                            break
                    elif kind == "snapshot":
                        # Empty snapshot is valid; keep streaming.
                        continue
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
