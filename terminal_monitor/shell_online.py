"""Optional shell.online read-only provider adapter.

Never auto-installed. Shares only ``terminal-monitor attach --read-only`` so a
remote browser cannot type into the managed PTY. The E2EE browser password is
returned once to the invoking operator and never persisted.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .remote import RemoteShare

PROVIDER_NAME = "shell.online"
SHARE_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class ShellOnlineLaunchResult:
    share: RemoteShare
    browser_password: str


def _resolve_binary(binary: str | None = None) -> str | None:
    if binary:
        candidate = shutil.which(binary) or (binary if Path(binary).is_file() else None)
        return candidate
    return shutil.which("shell")


def _looks_like_shell_online(version_output: str) -> bool:
    lowered = version_output.lower()
    return "shell" in lowered and ("online" in lowered or "shell.online" in lowered or "v" in lowered)


class ShellOnlineProvider:
    def __init__(self, binary: str | None = None) -> None:
        self._binary_override = binary

    def _binary(self) -> str | None:
        return _resolve_binary(self._binary_override)

    def available(self) -> tuple[bool, str]:
        binary = self._binary()
        if not binary:
            return False, "E_REMOTE_PROVIDER_UNAVAILABLE: shell.online CLI ('shell') is not installed."
        try:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10.0,
            )
        except subprocess.TimeoutExpired:
            return False, "E_REMOTE_PROVIDER_UNAVAILABLE: shell.online version probe timed out."
        except OSError as exc:
            return False, f"E_REMOTE_PROVIDER_UNAVAILABLE: cannot execute shell.online CLI: {exc}"
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
        if proc.returncode != 0:
            return False, f"E_REMOTE_PROVIDER_UNAVAILABLE: shell.online version probe failed: {combined[:300]}"
        if not _looks_like_shell_online(combined):
            return False, "E_REMOTE_PROVIDER_UNRECOGNIZED_BINARY: The resolved `shell` executable could not be verified as shell.online."
        return True, combined[:300] or "shell.online available"

    def build_share_command(self, *, state_dir: str) -> list[str]:
        binary = self._binary() or "shell"
        resolved_state = str(Path(state_dir).resolve())
        # Prefer `python -m terminal_monitor attach` for deterministic installs.
        attach = [sys.executable, "-m", "terminal_monitor", "attach", "--state-dir", resolved_state, "--read-only"]
        return [binary, "--read-only", "--json", "--", *attach]

    @staticmethod
    def parse_metadata(payload: dict[str, Any]) -> tuple[RemoteShare, str]:
        try:
            share_url = str(payload.get("share_url") or payload.get("url") or "")
            session_id = str(payload.get("session_id") or payload.get("id") or "")
            password = str(payload.get("e2ee_password") or payload.get("password") or payload.get("browser_password") or "")
            encrypted = bool(payload.get("encrypted", True))
            read_only = bool(payload.get("read_only", payload.get("readOnly", True)))
        except (AttributeError, ValueError, TypeError) as exc:
            raise ValueError(f"E_REMOTE_SHARE_FAILED: invalid shell.online metadata: {exc}") from exc
        if not share_url or not session_id or not password:
            raise ValueError("E_REMOTE_SHARE_FAILED: shell.online metadata is missing share_url/session_id/password.")
        if not read_only:
            raise ValueError("E_REMOTE_SHARE_FAILED: refusing non-read-only remote share.")
        share = RemoteShare(provider=PROVIDER_NAME, session_id=session_id, share_url=share_url, encrypted=encrypted, read_only=True)
        return share, password

    def share_read_only(self, *, state_dir: str) -> RemoteShare:
        # Compatibility with the RemoteProvider protocol: password is printed
        # by the CLI layer via share_read_only_with_password(); this method
        # keeps only non-secret metadata.
        result = self.share_read_only_with_password(state_dir=state_dir)
        return result.share

    def share_read_only_with_password(self, *, state_dir: str) -> ShellOnlineLaunchResult:
        ok, detail = self.available()
        if not ok:
            raise RuntimeError(detail)
        self._binary() or "shell"
        command = self.build_share_command(state_dir=state_dir)
        # Fail closed: read-only and JSON metadata are mandatory; never pass
        # --no-e2ee and never use shell=True.
        assert "--read-only" in command
        assert "--json" in command
        assert not any(part == "--no-e2ee" for part in command)
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=SHARE_TIMEOUT_SECONDS,
                cwd=str(Path(state_dir).resolve()),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("E_REMOTE_SHARE_FAILED: Read-only remote sharing could not be created (timed out).") from exc
        except OSError as exc:
            raise RuntimeError(f"E_REMOTE_SHARE_FAILED: Read-only remote sharing could not be created: {exc}") from exc
        if proc.returncode != 0:
            tail = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()[-500:]
            raise RuntimeError(f"E_REMOTE_SHARE_FAILED: Read-only remote sharing could not be created. {tail}")
        metadata: dict[str, Any] = {}
        # shell --json may emit log lines plus one JSON object; take the last
        # parseable JSON object line.
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                metadata = candidate
        if not metadata:
            # Fall back to whole-output parse for compact test fixtures.
            try:
                candidate = json.loads(proc.stdout or "")
                if isinstance(candidate, dict):
                    metadata = candidate
            except json.JSONDecodeError:
                pass
        if not metadata:
            raise RuntimeError("E_REMOTE_SHARE_FAILED: shell.online did not return JSON share metadata.")
        share, password = self.parse_metadata(metadata)
        return ShellOnlineLaunchResult(share=share, browser_password=password)

    def stop(self, session_id: str) -> tuple[bool, str]:
        if not session_id or not session_id.strip():
            return False, "E_REMOTE_SHARE_FAILED: session id is required"
        # Avoid shell=True; validate the id is a safe single token.
        cleaned = session_id.strip()
        if len(cleaned) > 200 or any(ch.isspace() for ch in cleaned):
            return False, "E_REMOTE_SHARE_FAILED: invalid session id"
        # shlex is used only for error messages; execution uses argv directly.
        _ = shlex.quote(cleaned)
        binary = self._binary()
        if not binary:
            return False, "E_REMOTE_PROVIDER_UNAVAILABLE: shell.online CLI ('shell') is not installed."
        try:
            proc = subprocess.run(
                [binary, "kill", cleaned],
                capture_output=True,
                text=True,
                check=False,
                timeout=STOP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False, "E_REMOTE_SHARE_FAILED: stop timed out"
        except OSError as exc:
            return False, f"E_REMOTE_SHARE_FAILED: {exc}"
        if proc.returncode != 0:
            detail = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()[-300:]
            return False, f"E_REMOTE_SHARE_FAILED: {detail or 'kill failed'}"
        return True, "stopped"
