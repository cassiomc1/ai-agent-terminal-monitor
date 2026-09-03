"""Terminal backends: Terminal.app, iTerm2, and tmux plus subprocess helpers."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safety import SPECIAL_KEY_CODES
from .types import TabResult


@dataclass(frozen=True)
class TerminalIdentity:
    """Hints used to choose the correct terminal among similar agent tabs."""

    project_path: str = ""
    branch: str = ""
    session_id: str = ""
    title: str = ""
    root_pid: int | None = None

    def score(self, candidate: dict[str, Any]) -> int:
        text = " ".join(str(candidate.get(key, "")) for key in ("history", "hist", "title", "wname"))
        score = 0
        if self.project_path and self.project_path in text:
            score += 8
        if self.session_id and self.session_id in text:
            score += 7
        if self.branch and self.branch in text:
            score += 5
        if self.title and self.title.lower() in str(candidate.get("title", "")).lower():
            score += 3
        if self.root_pid is not None and int(candidate.get("root_pid", -1)) == self.root_pid:
            score += 10
        return score
def validate_process_name(process: str) -> str:
    """Ensure process name contains only safe alphanumeric/dash/underscore characters."""
    clean = process.strip()
    if not clean or not re.match(r"^[A-Za-z0-9_.-]+$", clean):
        raise ValueError(f"Invalid process name: {process!r}")
    return clean


def validate_web_port(port: int) -> int:
    """Validate a localhost TCP port; zero requests an ephemeral port."""
    value = int(port)
    if not 0 <= value <= 65535:
        raise ValueError(f"Invalid web port: {port!r}; expected 0..65535")
    return value


def validate_title_filter(title: str | None) -> str | None:
    """Normalize and sanity-check a window title substring filter.

    Rejects newlines/control characters and caps length so the value stays a
    safe single-line AppleScript string literal (escaping still applied later).
    """
    if title is None:
        return None
    clean = title.strip()
    if not clean:
        return None
    if len(clean) > 200:
        raise ValueError("Title filter too long (max 200 characters)")
    if re.search(r"[\x00-\x1f\x7f]", clean):
        raise ValueError(f"Title filter contains control characters: {title!r}")
    return clean


def applescript_escape(value: str) -> str:
    """Escape backslashes and double quotes for AppleScript literal strings."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def applescript_terminal_title_condition(title: str | None) -> str:
    """Prefer a tab's custom title before falling back to its window name.

    Terminal.app appends the application name to every window name.  A broad
    filter such as ``OpenCode`` therefore matched an unrelated OpenCode tab
    whose custom title was ``OC | ...``.  Only tabs without a custom title may
    use the window-name fallback.
    """
    checked_title = validate_title_filter(title)
    if not checked_title:
        return "set titleOK to true"
    wanted = applescript_escape(checked_title)
    return f'set titleOK to ((ttitle contains "{wanted}") or (ttitle is "" and wname contains "{wanted}"))'


OSASCRIPT_TIMEOUT_SECONDS = 15.0
COMMAND_TIMEOUT_SECONDS = 30.0


def run_osascript_timeout_message() -> str:
    """Return the standardized error message emitted when osascript times out."""
    return f"osascript timed out after {int(OSASCRIPT_TIMEOUT_SECONDS)}s"


def run_osascript(script: str) -> tuple[int, str]:
    """Execute AppleScript via osascript subprocess safely with a hard timeout."""
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=OSASCRIPT_TIMEOUT_SECONDS,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return proc.returncode, out or err
    except subprocess.TimeoutExpired:
        return 1, run_osascript_timeout_message()
    except Exception as exc:
        return 1, str(exc)


def run_command(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a local command and return returncode, stdout, stderr with a hard timeout."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"command timed out after {int(COMMAND_TIMEOUT_SECONDS)}s"
    except Exception as exc:
        return 1, "", str(exc)


def parse_tab_output(raw: str) -> TabResult:
    """Parse key-value output emitted by AppleScript tab queries."""
    if raw.strip() == "MISSING":
        return {"ok": False, "error": "matching Terminal tab not found"}

    data: dict[str, str | bool] = {"ok": True, "error": ""}
    lines = raw.splitlines()
    hist_lines: list[str] = []
    in_hist = False

    for line in lines:
        if in_hist:
            hist_lines.append(line)
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            if key == "WIN":
                data["win"] = val
            elif key == "TAB":
                data["tab"] = val
            elif key == "TITLE":
                data["title"] = val
            elif key == "BUSY":
                data["busy"] = val.lower() == "true"
            elif key == "WNAME":
                data["wname"] = val
            elif key == "HIST":
                in_hist = True
                hist_lines.append(val)

    data["hist"] = "\n".join(hist_lines)
    return data  # type: ignore[return-value]


class BaseTerminalBackend:
    """Abstract base class for terminal interaction backends."""

    @property
    def owns_process(self) -> bool:
        return False

    def name(self) -> str:
        raise NotImplementedError

    def get_tab(self, process: str, title: str | None = None) -> TabResult:
        raise NotImplementedError

    def get_tab_for_identity(self, process: str, identity: TerminalIdentity) -> TabResult:
        """Resolve a tab using progressively broader stable identity hints."""
        hints = [
            identity.title,
            identity.session_id,
            identity.branch,
            Path(identity.project_path).name if identity.project_path else "",
        ]
        seen: set[str | None] = set()
        for hint in [*hints, None]:
            normalized = hint or None
            if normalized in seen:
                continue
            seen.add(normalized)
            tab = self.get_tab(process, normalized)
            if tab.get("ok"):
                return tab
        return TabResult(ok=False, error="matching terminal identity not found")

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        raise NotImplementedError

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        """Send a special key or control sequence to the target tab."""
        return self.send(process, title, key)

    def get_pids(self, process: str) -> list[int]:
        """Return list of active process IDs matching process name."""
        process = validate_process_name(process)
        if not shutil.which("pgrep"):
            return []
        code, out, _ = run_command(["pgrep", "-x", process])
        if code != 0 or not out:
            return []
        pids: list[int] = []
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids


class TerminalAppBackend(BaseTerminalBackend):
    """Native macOS Terminal.app backend via AppleScript."""

    def name(self) -> str:
        return "terminal"

    def get_tab(self, process: str, title: str | None = None) -> TabResult:
        process = validate_process_name(process)
        process_literal = applescript_escape(process)
        title_check = applescript_terminal_title_condition(title)
        script = f'''
tell application "Terminal"
  repeat with w from 1 to count of windows
    repeat with t from 1 to count of tabs of window w
      if ((processes of tab t of window w) as string) contains "{process_literal}" then
        set ttitle to ""
        try
          set ttitle to (custom title of tab t of window w) as string
        end try
        set wname to (name of window w) as string
        {title_check}
        if titleOK then
          return "WIN=" & w & linefeed & "TAB=" & t & linefeed & "TITLE=" & ttitle & linefeed & "BUSY=" & ((busy of tab t of window w) as string) & linefeed & "WNAME=" & wname & linefeed & "HIST=" & ((history of tab t of window w) as string)
        end if
      end if
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        if code:
            return {"ok": False, "error": output}
        return parse_tab_output(output)

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        process_literal = applescript_escape(validate_process_name(process))
        title_check = applescript_terminal_title_condition(title)
        escaped_payload = applescript_escape(re.sub(r"\s+", " ", payload).strip())
        script = f'''
tell application "Terminal"
  repeat with w from 1 to count of windows
    repeat with t from 1 to count of tabs of window w
      if ((processes of tab t of window w) as string) contains "{process_literal}" then
        set ttitle to ""
        try
          set ttitle to (custom title of tab t of window w) as string
        end try
        set wname to (name of window w) as string
        {title_check}
        if titleOK then
          do script "{escaped_payload}" in tab t of window w
          return "SENT"
        end if
      end if
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        return code == 0 and output == "SENT", output

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        """Send a special key code directly via native AppleScript without System Events."""
        process_literal = applescript_escape(validate_process_name(process))
        key_normalized = key.lower().strip()
        char_id = SPECIAL_KEY_CODES.get(key_normalized)
        if char_id is None:
            if len(key) == 1:
                char_id = ord(key)
            else:
                return self.send(process, title, key)

        title_check = applescript_terminal_title_condition(title)
        script = f'''
tell application "Terminal"
  repeat with w from 1 to count of windows
    repeat with t from 1 to count of tabs of window w
      if ((processes of tab t of window w) as string) contains "{process_literal}" then
        set ttitle to ""
        try
          set ttitle to (custom title of tab t of window w) as string
        end try
        set wname to (name of window w) as string
        {title_check}
        if titleOK then
          do script (character id {char_id}) in tab t of window w
          return "SENT"
        end if
      end if
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        return code == 0 and output == "SENT", output


class ITerm2Backend(BaseTerminalBackend):
    """Native macOS iTerm2 backend via AppleScript."""

    def name(self) -> str:
        return "iterm2"

    def get_tab(self, process: str, title: str | None = None) -> TabResult:
        process_literal = applescript_escape(validate_process_name(process))
        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
            title_check = f'set titleOK to (sname contains "{wanted}")'
        script = f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (variable named "commandLine" of s as string) contains "{process_literal}" or (name of s as string) contains "{process_literal}" then
          set sname to (name of s) as string
          {title_check}
          if titleOK then
            return "WIN=" & (id of w) & linefeed & "TAB=" & (id of t) & linefeed & "TITLE=" & sname & linefeed & "BUSY=false" & linefeed & "WNAME=" & sname & linefeed & "HIST=" & (text of s as string)
          end if
        end if
      end repeat
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        if code:
            return {"ok": False, "error": output}
        return parse_tab_output(output)

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        process_literal = applescript_escape(validate_process_name(process))
        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
            title_check = f'set titleOK to (sname contains "{wanted}")'
        escaped_payload = applescript_escape(re.sub(r"\s+", " ", payload).strip())
        script = f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (variable named "commandLine" of s as string) contains "{process_literal}" or (name of s as string) contains "{process_literal}" then
          set sname to (name of s) as string
          {title_check}
          if titleOK then
            tell s to write text "{escaped_payload}"
            return "SENT"
          end if
        end if
      end repeat
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        return code == 0 and output == "SENT", output

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        """Send a special key code directly via iTerm2 write text without newline."""
        process_literal = applescript_escape(validate_process_name(process))
        key_normalized = key.lower().strip()
        char_id = SPECIAL_KEY_CODES.get(key_normalized)
        if char_id is None:
            if len(key) == 1:
                char_id = ord(key)
            else:
                return self.send(process, title, key)

        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
            title_check = f'set titleOK to (sname contains "{wanted}")'
        script = f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (variable named "commandLine" of s as string) contains "{process_literal}" or (name of s as string) contains "{process_literal}" then
          set sname to (name of s) as string
          {title_check}
          if titleOK then
            tell s to write text (character id {char_id}) without newline
            return "SENT"
          end if
        end if
      end repeat
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        return code == 0 and output == "SENT", output


class TmuxBackend(BaseTerminalBackend):
    """Cross-platform terminal backend using tmux capture-pane and send-keys."""

    def name(self) -> str:
        return "tmux"

    def _find_target(self, process: str, title: str | None = None) -> str | None:
        if not shutil.which("tmux"):
            return None
        title = validate_title_filter(title)
        code, out, _ = run_command(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_title}"])
        if code != 0 or not out:
            return None
        for line in out.splitlines():
            parts = line.strip().split(maxsplit=2)
            if not parts:
                continue
            target = parts[0]
            cmd = parts[1] if len(parts) > 1 else ""
            pane_title = parts[2] if len(parts) > 2 else ""
            if (process.lower() in cmd.lower() or process.lower() in pane_title.lower()) and (
                not title or title.lower() in pane_title.lower()
            ):
                return target
        return None

    def get_tab(self, process: str, title: str | None = None) -> TabResult:
        target = self._find_target(process, title)
        if not target:
            return {"ok": False, "error": "matching tmux pane not found"}
        code, out, err = run_command(["tmux", "capture-pane", "-p", "-t", target, "-S", "-300"])
        if code != 0:
            return {"ok": False, "error": err or "tmux capture-pane failed"}
        return {
            "ok": True,
            "error": "",
            "win": target,
            "tab": target,
            "title": target,
            "busy": False,
            "wname": target,
            "hist": out,
        }

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        target = self._find_target(process, title)
        if not target:
            return False, "matching tmux pane not found"
        clean = re.sub(r"\s+", " ", payload).strip()
        code, out, err = run_command(["tmux", "send-keys", "-t", target, clean, "Enter"])
        return code == 0, "SENT" if code == 0 else err or out

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        target = self._find_target(process, title)
        if not target:
            return False, "matching tmux pane not found"
        tmux_key_map = {
            "tab": "Tab",
            "\t": "Tab",
            "enter": "Enter",
            "return": "Enter",
            "\r": "Enter",
            "\n": "Enter",
            "esc": "Escape",
            "escape": "Escape",
            "\x1b": "Escape",
            "ctrl+c": "C-c",
            "ctrl_c": "C-c",
            "ctrl+p": "C-p",
            "ctrl_p": "C-p",
            "ctrl+d": "C-d",
            "ctrl_d": "C-d",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "space": "Space",
            "backspace": "BSpace",
        }
        tmux_key = tmux_key_map.get(key.lower().strip(), key)
        code, out, err = run_command(["tmux", "send-keys", "-t", target, tmux_key])
        return code == 0, "SENT" if code == 0 else err or out


def get_backend(backend_name: str = "auto") -> BaseTerminalBackend:
    """Resolve terminal backend instance by name or environment."""
    choice = backend_name.lower().strip()
    if choice == "auto":
        if os.environ.get("TMUX") and shutil.which("tmux"):
            return TmuxBackend()
        if sys.platform == "darwin":
            return TerminalAppBackend()
        if shutil.which("tmux"):
            return TmuxBackend()
        return TerminalAppBackend()

    if choice in ("terminal", "terminal.app", "apple"):
        return TerminalAppBackend()
    if choice in ("iterm", "iterm2"):
        return ITerm2Backend()
    if choice == "tmux":
        return TmuxBackend()
    if choice in ("pty", "managed", "managed-pty"):
        from .managed_pty import ManagedPTYBackend

        return ManagedPTYBackend()

    raise ValueError(f"Unknown terminal backend: {backend_name}. Available: auto, terminal, iterm2, tmux, pty")


# Backward-compatible function wrappers
def terminal_tab(process: str, title: str | None = None) -> TabResult:
    """Inspect terminal tab using the default Terminal.app backend."""
    return TerminalAppBackend().get_tab(process, title)


def process_pids(process: str) -> list[int]:
    """Return PIDs matching process name."""
    return TerminalAppBackend().get_pids(process)


def send_to_terminal(process: str, title: str | None, payload: str) -> tuple[bool, str]:
    """Send text to Terminal.app tab."""
    return TerminalAppBackend().send(process, title, payload)
