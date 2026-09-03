"""Optional shell.online read-only provider adapter.

Never auto-installed. Shares only ``terminal-monitor attach --read-only`` so a
remote browser cannot type into the managed PTY. The E2EE browser password is
returned once to the invoking operator and never persisted.
"""
from __future__ import annotations

import json
import re
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

# Upstream prints e.g. ``shell 0.7.3`` (stdout). Accept release and suffixed
# development builds, but nothing else: a bare ``v`` prefix, a missing patch
# component, or an arbitrary binary mentioning "shell" must not validate.
SHELL_VERSION_PATTERN = re.compile(r"^shell\s+(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ShellOnlineLaunchResult:
    share: RemoteShare
    browser_password: str


def _resolve_binary(binary: str | None = None) -> str | None:
    if binary:
        candidate = shutil.which(binary) or (binary if Path(binary).is_file() else None)
        return candidate
    return shutil.which("shell")


def parse_shell_version(output: str) -> str | None:
    """Return the version from ``shell --version`` output, else None."""
    for line in (output or "").splitlines():
        match = SHELL_VERSION_PATTERN.match(line.strip())
        if match:
            return match.group(1)
    return None


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
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if proc.returncode != 0:
            return False, "E_REMOTE_PROVIDER_UNAVAILABLE: shell.online version probe failed."
        version = parse_shell_version(combined)
        if version is None:
            return False, "E_REMOTE_PROVIDER_UNRECOGNIZED_BINARY: The resolved `shell` executable could not be verified as shell.online."
        return True, f"shell.online {version}"

    def build_share_command(self, *, state_dir: str) -> list[str]:
        binary = self._binary() or "shell"
        resolved_state = str(Path(state_dir).resolve())
        # Prefer `python -m terminal_monitor attach` for deterministic installs.
        attach = [sys.executable, "-m", "terminal_monitor", "attach", "--state-dir", resolved_state, "--read-only"]
        return [binary, "--read-only", "--json", "--", *attach]

    @staticmethod
    def extract_session_event(*outputs: str) -> dict[str, Any]:
        """Find the ``type == session`` JSON event, preferring stderr.

        The real CLI emits the session event on stderr amid diagnostic
        lines; stdout carries no session metadata. Tolerates surrounding
        noise by scanning line-delimited JSON candidates.
        """
        for output in outputs:
            for line in (output or "").splitlines():
                candidate_text = line.strip()
                if not candidate_text.startswith("{"):
                    continue
                try:
                    candidate = json.loads(candidate_text)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get("type") == "session":
                    return candidate
        raise ValueError("E_REMOTE_SHARE_FAILED: shell.online did not emit a session event.")

    @staticmethod
    def parse_metadata(payload: dict[str, Any]) -> tuple[RemoteShare, str]:
        """Validate a session event; missing security fields fail closed."""
        if not isinstance(payload, dict) or payload.get("type") != "session":
            raise ValueError("E_REMOTE_SHARE_FAILED: shell.online metadata is not a session event.")
        try:
            share_url = str(payload.get("share_url") or payload.get("url") or "")
            session_id = str(payload.get("session_id") or payload.get("id") or "")
            password = str(payload.get("e2ee_password") or "")
        except (AttributeError, ValueError, TypeError) as exc:
            raise ValueError("E_REMOTE_SHARE_FAILED: invalid shell.online metadata.") from exc
        if not share_url or not session_id:
            raise ValueError("E_REMOTE_SHARE_FAILED: shell.online metadata is missing share_url/session_id.")
        # E2EE and read-only are mandatory: absent or false fails closed.
        if payload.get("encrypted") is not True or payload.get("read_only") is not True:
            raise ValueError("E_REMOTE_SHARE_INSECURE: refusing remote share without explicit encrypted/read-only metadata.")
        if not password:
            raise ValueError("E_REMOTE_SHARE_FAILED: shell.online metadata is missing the E2EE password.")
        share = RemoteShare(provider=PROVIDER_NAME, session_id=session_id, share_url=share_url, encrypted=True, read_only=True)
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
        command = self.build_share_command(state_dir=state_dir)
        # Fail closed: read-only and JSON metadata are mandatory; E2EE stays
        # on and execution never goes through a shell.
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
            raise RuntimeError("E_REMOTE_SHARE_FAILED: Read-only remote sharing could not be created.") from exc
        if proc.returncode != 0:
            # Never echo raw provider output: it may carry secrets.
            raise RuntimeError("E_REMOTE_SHARE_FAILED: Read-only remote sharing could not be created.")
        try:
            event = self.extract_session_event(proc.stderr or "", proc.stdout or "")
            share, password = self.parse_metadata(event)
        except ValueError as exc:
            # Re-raise the machine-readable code without any provider payload.
            message = str(exc)
            code = message.split(":", 1)[0] if ":" in message else "E_REMOTE_SHARE_FAILED"
            raise RuntimeError(code + ": Read-only remote sharing could not be created.") from None
        return ShellOnlineLaunchResult(share=share, browser_password=password)

    def stop(self, session_id: str) -> tuple[bool, str]:
        if not session_id or not session_id.strip():
            return False, "E_REMOTE_SHARE_FAILED: session id is required"
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
            return False, "E_REMOTE_SHARE_FAILED: provider kill failed"
        return True, "stopped"
