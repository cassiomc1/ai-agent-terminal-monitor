#!/usr/bin/env python3
"""Monitor and safely nudge any AI CLI agent running in macOS Terminal.app, iTerm2, or tmux.

Generic and extensible across any agent (Claude Code, OpenCode, Aider, Goose, etc.)
and any project via configuration files, profiles, and customizable rules.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple

# Ensure module is registered in sys.modules for Python 3.14+ dataclasses when loaded via importlib
if __name__ not in sys.modules:
    sys.modules[__name__] = sys.modules.get("__main__", types.ModuleType(__name__))

# Optional TOML support (standard in Python 3.11+)
try:
    import tomllib
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None


# ---------------------------------------------------------------------------
# Default Safety and Decision Rules
# ---------------------------------------------------------------------------

UNSAFE_PHRASES: tuple[str, ...] = (
    "bypass",
    "delete",
    "disable validator",
    "discard",
    "drop database",
    "drop table",
    "force",
    "format disk",
    "hard reset",
    "no-verify",
    "overwrite",
    "purge",
    "remove protection",
    "reset --hard",
    "rm -rf",
    "skip validation",
    "weaken",
)

DEFAULT_PREFERRED_ANSWERS: tuple[str, ...] = (
    "continue",
    "proceed",
    "keep",
    "inspect",
    "validate",
    "fail closed",
    "yes",
    "allow",
    "approve",
)


def match_pattern(pattern: str, text: str) -> bool:
    """Match a pattern against text safely, supporting regex or substring."""
    low_text = text.lower()
    low_pat = pattern.lower()
    if low_pat in low_text:
        return True
    if any(token in pattern for token in (r"\b", r"\s", r"\d", r"\S", r"\w", "^", "$", "|", ".*", ".+", "?:")):
        try:
            return bool(re.search(pattern, text, re.IGNORECASE | re.MULTILINE))
        except re.error:
            pass
    return False


# ---------------------------------------------------------------------------
# Agent Profiles
# ---------------------------------------------------------------------------

@dataclass
class AgentProfile:
    """Configuration profile describing how to detect and interact with an AI agent CLI."""

    name: str
    process: str = "generic"
    description: str = ""
    thinking_patterns: list[str] = field(default_factory=list)
    permission_patterns: list[str] = field(default_factory=list)
    question_indicators: list[str] = field(default_factory=list)
    option_patterns: list[str] = field(default_factory=list)
    unsafe_phrases: list[str] = field(default_factory=lambda: list(UNSAFE_PHRASES))
    preferred_answers: list[str] = field(default_factory=lambda: list(DEFAULT_PREFERRED_ANSWERS))
    auto_permission_payload: str = ""
    default_continue_text: str | None = None

    def matches_thinking(self, history_tail: str) -> bool:
        return any(match_pattern(pat, history_tail) for pat in self.thinking_patterns)

    def matches_permission(self, history_tail: str) -> bool:
        return any(match_pattern(pat, history_tail) for pat in self.permission_patterns)

    def matches_question(self, history_tail: str) -> bool:
        if any(match_pattern(pat, history_tail) for pat in self.question_indicators):
            return True
        # If multiple structured options are present in tail
        if len(self.extract_options(history_tail)) >= 2:
            return True
        return False

    def extract_options(self, history_tail: str) -> list[tuple[str, bool]]:
        options: list[tuple[str, bool]] = []
        for line in history_tail.splitlines():
            is_option = any(match_pattern(pat, line) for pat in self.option_patterns)
            if not is_option and re.search(r"recommended", line, re.IGNORECASE):
                is_option = True

            if is_option:
                clean = clean_option(line)
                if clean:
                    is_rec = bool(re.search(r"recommended", line, re.IGNORECASE))
                    options.append((clean, is_rec))
        return options


BUILTIN_PROFILES: dict[str, AgentProfile] = {
    "opencode": AgentProfile(
        name="opencode",
        process="opencode",
        description="Profile for OpenCode agent CLI",
        thinking_patterns=[
            "esc interrupt",
            "preparing write",
            "thinking...",
            "working...",
        ],
        permission_patterns=[
            r"allow.*deny",
            "allow once",
            "permission required",
        ],
        question_indicators=[
            "(recommended)",
            r"^\s*[●○◉]\s+\S",
            r"\b(choose|select|which option|pick one|what should)\b",
        ],
        option_patterns=[
            r"^\s*[●○◉]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="",
    ),
    "claude": AgentProfile(
        name="claude",
        process="claude",
        description="Profile for Anthropic Claude Code CLI",
        thinking_patterns=[
            "thinking...",
            "thinking",
            "esc interrupt",
            "esc to cancel",
            "running tool",
            "reading file",
            "writing file",
            "running command",
            "waiting for response",
            "fetching...",
        ],
        permission_patterns=[
            "allow once",
            "allow this tool",
            "allow always",
            "do you want to run",
            "[y/n]",
            "yes / no",
            "approve tool",
            "press enter to continue",
        ],
        question_indicators=[
            "(recommended)",
            r"\b(select|choose|which option|pick)\b",
            r"\b(approve|deny|reject)\b",
        ],
        option_patterns=[
            r"^\s*[●○◉❯>]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="y",
    ),
    "claude-code": AgentProfile(
        name="claude-code",
        process="claude",
        description="Alias for Anthropic Claude Code CLI",
        thinking_patterns=[
            "thinking...",
            "thinking",
            "esc interrupt",
            "esc to cancel",
            "running tool",
            "reading file",
            "writing file",
            "running command",
            "waiting for response",
        ],
        permission_patterns=[
            "allow once",
            "allow this tool",
            "allow always",
            "do you want to run",
            "[y/n]",
            "yes / no",
            "approve tool",
        ],
        question_indicators=[
            "(recommended)",
            r"\b(select|choose|which option|pick)\b",
        ],
        option_patterns=[
            r"^\s*[●○◉❯>]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="y",
    ),
    "aider": AgentProfile(
        name="aider",
        process="aider",
        description="Profile for Aider pair programming CLI",
        thinking_patterns=[
            "thinking...",
            "analyzing",
            "generating code",
            "processing",
            "updating repo",
            "indexing",
            "searching",
        ],
        permission_patterns=[
            "run command?",
            "(y)es/(n)o",
            "apply changes?",
            "add them to the chat?",
            "create a new file?",
            "run the test command?",
        ],
        question_indicators=[
            r"\(y\)es/\(n\)o",
            r"\[yes\]:",
            "(recommended)",
        ],
        option_patterns=[
            r"^\s*\d+[.)\]]\s+\S",
            r"^\s*[●○◉]\s+\S",
        ],
        auto_permission_payload="y",
    ),
    "goose": AgentProfile(
        name="goose",
        process="goose",
        description="Profile for Block Goose AI agent CLI",
        thinking_patterns=[
            "thinking...",
            "working...",
            "calling tool...",
            "executing...",
        ],
        permission_patterns=[
            "permission required",
            "approve",
            "deny",
            "allow this action",
        ],
        question_indicators=[
            "(recommended)",
            r"\b(choose|select|pick)\b",
        ],
        option_patterns=[
            r"^\s*[●○◉]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="y",
    ),
    "generic": AgentProfile(
        name="generic",
        process="agent",
        description="Generic fallback profile for any AI CLI agent",
        thinking_patterns=[
            "thinking...",
            "esc interrupt",
            "preparing write",
            "working...",
            "please wait",
            "processing...",
            "running...",
        ],
        permission_patterns=[
            r"allow.*deny",
            "allow once",
            "permission required",
            "permission",
            "approve",
            "[y/n]",
            "allow this action",
        ],
        question_indicators=[
            "(recommended)",
            r"^\s*[●○◉]\s+\S",
            r"\b(choose|select|which option|pick one|what should)\b",
        ],
        option_patterns=[
            r"^\s*[●○◉]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="",
    ),
}


def get_profile(name_or_process: str | None = None, custom_profiles: dict[str, Any] | None = None) -> AgentProfile:
    """Resolve an AgentProfile by name, process, or dictionary config."""
    custom = custom_profiles or {}
    key = (name_or_process or "generic").lower().strip()

    # Match in custom profiles first
    if key in custom:
        val = custom[key]
        if isinstance(val, AgentProfile):
            return val
        if isinstance(val, dict):
            return AgentProfile(
                name=key,
                process=val.get("process", key),
                description=val.get("description", ""),
                thinking_patterns=val.get("thinking_patterns", []),
                permission_patterns=val.get("permission_patterns", []),
                question_indicators=val.get("question_indicators", []),
                option_patterns=val.get("option_patterns", [r"^\s*[●○◉]\s+\S", r"^\s*\d+[.)\]]\s+\S"]),
                unsafe_phrases=val.get("unsafe_phrases", list(UNSAFE_PHRASES)),
                preferred_answers=val.get("preferred_answers", list(DEFAULT_PREFERRED_ANSWERS)),
                auto_permission_payload=val.get("auto_permission_payload", ""),
                default_continue_text=val.get("default_continue_text"),
            )

    # Match in built-in profiles
    if key in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[key]

    # Try matching process name against built-in profiles
    for prof in BUILTIN_PROFILES.values():
        if prof.process.lower() == key:
            return prof

    # Fallback generic profile with the requested process name
    generic = BUILTIN_PROFILES["generic"]
    return AgentProfile(
        name=key,
        process=key,
        description=f"Auto-generated profile for {key}",
        thinking_patterns=list(generic.thinking_patterns),
        permission_patterns=list(generic.permission_patterns),
        question_indicators=list(generic.question_indicators),
        option_patterns=list(generic.option_patterns),
        unsafe_phrases=list(generic.unsafe_phrases),
        preferred_answers=list(generic.preferred_answers),
        auto_permission_payload=generic.auto_permission_payload,
    )


def list_profiles(custom_profiles: dict[str, Any] | None = None) -> dict[str, str]:
    """Return dictionary of all available profiles and their descriptions."""
    profiles: dict[str, str] = {}
    for name, prof in BUILTIN_PROFILES.items():
        profiles[name] = prof.description or f"Profile for {name}"
    if custom_profiles:
        for name, data in custom_profiles.items():
            desc = data.get("description", "Custom profile") if isinstance(data, dict) else getattr(data, "description", "Custom profile")
            profiles[name] = desc
    return profiles


# ---------------------------------------------------------------------------
# Configuration Class
# ---------------------------------------------------------------------------

class Config(NamedTuple):
    """Configuration tuple for backward compatibility."""
    process: str
    title: str | None
    continue_text: str
    poll_seconds: float
    idle_seconds: float
    cooldown_seconds: float
    gone_seconds: float
    max_sends: int
    auto_allow_permissions: bool
    once: bool
    dry_run: bool
    state_dir: str


@dataclass
class MonitorConfig:
    """Comprehensive and modular monitor configuration."""

    process: str = "opencode"
    profile: str = "opencode"
    title: str | None = None
    continue_text: str = ""
    continue_file: str | None = None
    poll_seconds: float = 3.0
    idle_seconds: float = 15.0
    cooldown_seconds: float = 20.0
    gone_seconds: float = 25.0
    max_sends: int = 100
    auto_allow_permissions: bool = False
    once: bool = False
    dry_run: bool = False
    state_dir: str = "/tmp/terminal-monitor"
    backend: str = "auto"
    project_dir: str = "."
    unsafe_phrases: list[str] = field(default_factory=lambda: list(UNSAFE_PHRASES))
    custom_profiles: dict[str, Any] = field(default_factory=dict)

    def to_legacy_config(self) -> Config:
        return Config(
            process=self.process,
            title=self.title,
            continue_text=self.continue_text,
            poll_seconds=self.poll_seconds,
            idle_seconds=self.idle_seconds,
            cooldown_seconds=self.cooldown_seconds,
            gone_seconds=self.gone_seconds,
            max_sends=self.max_sends,
            auto_allow_permissions=self.auto_allow_permissions,
            once=self.once,
            dry_run=self.dry_run,
            state_dir=self.state_dir,
        )


# ---------------------------------------------------------------------------
# Terminal Backends
# ---------------------------------------------------------------------------

def validate_process_name(value: str) -> str:
    """Ensure process name contains only safe characters."""
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
        raise ValueError("process name may contain only letters, numbers, . _ + and -")
    return value


def applescript_escape(value: str) -> str:
    """Escape strings for AppleScript literals."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def run_osascript(source: str, timeout: float = 12.0) -> tuple[int, str]:
    """Execute an AppleScript string via osascript with a timeout."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", source],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "osascript timeout"
    except OSError as exc:
        return 1, str(exc)
    output = (proc.stdout or "").strip()
    error = (proc.stderr or "").strip()
    return proc.returncode, error or output if proc.returncode else output


def parse_tab_output(output: str) -> dict[str, str | bool]:
    """Parse tab metadata and history from AppleScript output."""
    if not output or output == "MISSING":
        return {"ok": False, "error": "matching Terminal tab not found"}
    head, marker, history = output.partition("\nHIST=")
    if not marker:
        return {"ok": False, "error": "unexpected Terminal response"}
    result: dict[str, str | bool] = {"ok": True, "error": "", "hist": history}
    for line in head.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            result[key.lower()] = value
    return result


class BaseTerminalBackend:
    """Abstract interface for terminal interaction."""

    def name(self) -> str:
        raise NotImplementedError

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        raise NotImplementedError

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        raise NotImplementedError

    def get_pids(self, process: str) -> list[int]:
        try:
            output = subprocess.check_output(
                ["pgrep", "-x", validate_process_name(process)], text=True
            )
        except (OSError, subprocess.CalledProcessError):
            return []
        return [int(item) for item in output.split() if item.isdigit()]


class TerminalAppBackend(BaseTerminalBackend):
    """Native macOS Terminal.app backend via AppleScript."""

    def name(self) -> str:
        return "terminal"

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        process = validate_process_name(process)
        process_literal = applescript_escape(process)
        title_check = "set titleOK to true"
        if title:
            wanted = applescript_escape(title)
            title_check = f'set titleOK to ((ttitle contains "{wanted}") or (wname contains "{wanted}"))'
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
        title_check = "set titleOK to true"
        if title:
            wanted = applescript_escape(title)
            title_check = f'set titleOK to ((ttitle contains "{wanted}") or (wname contains "{wanted}"))'
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


class ITerm2Backend(BaseTerminalBackend):
    """Native macOS iTerm2 backend via AppleScript."""

    def name(self) -> str:
        return "iterm2"

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        process = validate_process_name(process)
        process_literal = applescript_escape(process)
        title_check = "set titleOK to true"
        if title:
            wanted = applescript_escape(title)
            title_check = f'set titleOK to (sname contains "{wanted}")'

        script = f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        set sname to (name of s) as string
        set scmd to ""
        try
          set scmd to (current command of s) as string
        end try
        if (sname contains "{process_literal}") or (scmd contains "{process_literal}") then
          {title_check}
          if titleOK then
            set stext to (text of s) as string
            return "WIN=1" & linefeed & "TAB=1" & linefeed & "TITLE=" & sname & linefeed & "BUSY=false" & linefeed & "HIST=" & stext
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
        title_check = "set titleOK to true"
        if title:
            wanted = applescript_escape(title)
            title_check = f'set titleOK to (sname contains "{wanted}")'
        escaped_payload = applescript_escape(re.sub(r"\s+", " ", payload).strip())

        script = f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        set sname to (name of s) as string
        set scmd to ""
        try
          set scmd to (current command of s) as string
        end try
        if (sname contains "{process_literal}") or (scmd contains "{process_literal}") then
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


class TmuxBackend(BaseTerminalBackend):
    """tmux backend (cross-platform, works on macOS, Linux, WSL, remote servers)."""

    def name(self) -> str:
        return "tmux"

    def _find_pane(self, process: str, title: str | None = None) -> str | None:
        if not shutil.which("tmux"):
            return None
        try:
            cmd = ["tmux", "list-panes", "-a", "-F", "#{pane_id}:#{pane_current_command}:#{window_name}:#{pane_title}"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            return None

        process_clean = process.lower()
        title_clean = (title or "").lower()

        for line in out.splitlines():
            parts = line.strip().split(":", 3)
            if len(parts) >= 2:
                pane_id, pane_cmd = parts[0], parts[1].lower()
                w_name = parts[2].lower() if len(parts) > 2 else ""
                p_title = parts[3].lower() if len(parts) > 3 else ""

                if process_clean in pane_cmd or process_clean in w_name or process_clean in p_title:
                    if not title_clean or (title_clean in w_name or title_clean in p_title):
                        return pane_id
        return None

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        pane_id = self._find_pane(process, title)
        if not pane_id:
            return {"ok": False, "error": "matching tmux pane not found"}
        try:
            cmd = ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-200"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            return {
                "ok": True,
                "error": "",
                "hist": out,
                "tab": pane_id,
                "title": pane_id,
                "busy": "false",
            }
        except (subprocess.SubprocessError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        pane_id = self._find_pane(process, title)
        if not pane_id:
            return False, "matching tmux pane not found"
        try:
            clean_payload = re.sub(r"\s+", " ", payload).strip()
            if clean_payload:
                subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", clean_payload], check=True)
            subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], check=True)
            return True, "SENT"
        except (subprocess.SubprocessError, OSError) as exc:
            return False, str(exc)


def get_backend(backend_name: str = "auto") -> BaseTerminalBackend:
    """Return backend instance based on name or auto-detection."""
    choice = backend_name.lower().strip()
    if choice == "auto":
        if os.environ.get("TMUX") and shutil.which("tmux"):
            return TmuxBackend()
        if sys.platform == "darwin":
            return TerminalAppBackend()
        if shutil.which("tmux"):
            return TmuxBackend()
        return TerminalAppBackend()

    if choice in ("terminal", "terminal.app", "macos"):
        return TerminalAppBackend()
    if choice in ("iterm", "iterm2"):
        return ITerm2Backend()
    if choice == "tmux":
        return TmuxBackend()

    raise ValueError(f"Unknown terminal backend: {backend_name}. Available: auto, terminal, iterm2, tmux")


# Backward-compatible function wrappers
def terminal_tab(process: str, title: str | None = None) -> dict[str, str | bool]:
    """Inspect terminal tab using the default Terminal.app backend."""
    return TerminalAppBackend().get_tab(process, title)


def process_pids(process: str) -> list[int]:
    """Return PIDs matching process name."""
    return TerminalAppBackend().get_pids(process)


def send_to_terminal(process: str, title: str | None, payload: str) -> tuple[bool, str]:
    """Send text to Terminal.app tab."""
    return TerminalAppBackend().send(process, title, payload)


# ---------------------------------------------------------------------------
# Classification and Decision Engine
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_snapshot(history: str) -> str:
    """Clean and normalize history snapshot for state hashing."""
    text = re.sub(r"[ \t]+", " ", history)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-30:])


def classify_state(history: str, profile: AgentProfile | None = None) -> str:
    """Classify the current terminal state (thinking, permission, question, idle)."""
    prof = profile or BUILTIN_PROFILES["opencode"]
    tail = "\n".join(history.splitlines()[-50:])

    if prof.matches_thinking(tail):
        return "thinking"
    if prof.matches_permission(tail):
        return "permission"
    if prof.matches_question(tail):
        return "question"
    return "idle"


def clean_option(line: str) -> str:
    """Strip menu bullets, numbers, and recommended markers from an option string."""
    value = re.sub(r"^[\s│┃>*●○◉❯-]+", "", line).strip()
    value = re.sub(r"^\d+[.)\]]\s*", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s*\(recommended\)\s*", "", value, flags=re.IGNORECASE).strip()


def extract_options(history: str, profile: AgentProfile | None = None) -> list[tuple[str, bool]]:
    """Extract selectable options and their recommendation status from history."""
    prof = profile or BUILTIN_PROFILES["opencode"]
    tail = "\n".join(history.splitlines()[-60:])
    return prof.extract_options(tail)


def is_unsafe(value: str, unsafe_list: list[str] | tuple[str, ...] | None = None) -> bool:
    """Check if an option or command string matches known unsafe phrases."""
    phrases = unsafe_list if unsafe_list is not None else UNSAFE_PHRASES
    low = value.lower()
    return any(phrase.lower() in low for phrase in phrases)


def decide_question(history: str, profile: AgentProfile | None = None) -> str | None:
    """Select the best safe option if unambiguous, otherwise None."""
    prof = profile or BUILTIN_PROFILES["opencode"]
    options = [
        (value, recommended)
        for value, recommended in extract_options(history, prof)
        if not is_unsafe(value, prof.unsafe_phrases)
    ]
    if not options:
        return None

    # Pick recommended safe option
    for value, recommended in options:
        if recommended:
            return value

    # Pick preferred keywords
    for key in prof.preferred_answers:
        for value, _ in options:
            if key.lower() in value.lower():
                return value

    # Pick if only one safe option exists
    return options[0][0] if len(options) == 1 else None


def append_log(path: str, message: str) -> None:
    """Append a timestamped log line to file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{now_iso()} {message}\n")


def consume_manual_answer(path: str) -> str | None:
    """Read and delete a manual answer file if present."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        os.remove(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return value or None


# ---------------------------------------------------------------------------
# Config Loading (JSON, TOML, Project Discovery)
# ---------------------------------------------------------------------------

CONFIG_FILENAMES = (
    ".terminal-monitor.json",
    ".terminal-monitor.toml",
    "terminal-monitor.json",
    "terminal-monitor.toml",
)


def discover_config_file(project_dir: str = ".") -> Path | None:
    """Look for standard config files in project_dir or user home."""
    base = Path(project_dir).resolve()
    for name in CONFIG_FILENAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate

    # Check user global config
    user_config_dir = Path.home() / ".config" / "terminal-monitor"
    for name in ("config.json", "config.toml"):
        candidate = user_config_dir / name
        if candidate.is_file():
            return candidate

    return None


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Parse a JSON or TOML configuration file."""
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {file_path}")

    text = file_path.read_text(encoding="utf-8")
    ext = file_path.suffix.lower()

    if ext == ".json":
        return json.loads(text)
    if ext == ".toml":
        if tomllib is None:
            raise RuntimeError("TOML support requires Python 3.11+ or the 'tomli' package.")
        return tomllib.loads(text)

    # Try JSON first, then TOML
    try:
        return json.loads(text)
    except Exception:
        if tomllib is not None:
            return tomllib.loads(text)
        raise ValueError(f"Could not parse config file '{file_path}' as JSON.")


def generate_starter_config(format_type: str = "json") -> str:
    """Generate a starter configuration template string."""
    fmt = format_type.lower().strip()
    sample_data = {
        "profile": "claude",
        "process": "claude",
        "title": None,
        "backend": "auto",
        "continue_text": "Proceed from the next incomplete step. Stop if you need human guidance.",
        "poll_seconds": 3.0,
        "idle_seconds": 15.0,
        "cooldown_seconds": 20.0,
        "gone_seconds": 25.0,
        "max_sends": 100,
        "auto_allow_permissions": False,
        "state_dir": "/tmp/terminal-monitor",
        "unsafe_phrases": list(UNSAFE_PHRASES),
        "custom_profiles": {
            "my-agent": {
                "process": "myagent",
                "description": "Custom agent profile example",
                "thinking_patterns": ["agent is thinking...", "processing..."],
                "permission_patterns": ["do you authorize this action?"],
                "auto_permission_payload": "y",
            }
        },
    }

    if fmt == "json":
        return json.dumps(sample_data, indent=2, ensure_ascii=False) + "\n"
    elif fmt == "toml":
        return """# Terminal Monitor Configuration File
profile = "claude"
process = "claude"
backend = "auto"
continue_text = "Proceed from the next incomplete step. Stop if you need human guidance."
poll_seconds = 3.0
idle_seconds = 15.0
cooldown_seconds = 20.0
gone_seconds = 25.0
max_sends = 100
auto_allow_permissions = false
state_dir = "/tmp/terminal-monitor"
unsafe_phrases = [
  "bypass",
  "delete",
  "rm -rf",
  "reset --hard",
  "drop database"
]

[custom_profiles.my-agent]
process = "myagent"
description = "Custom agent profile example"
thinking_patterns = ["agent is thinking...", "processing..."]
permission_patterns = ["do you authorize this action?"]
auto_permission_payload = "y"
"""
    else:
        raise ValueError(f"Unsupported format: {format_type}. Use 'json' or 'toml'.")


# ---------------------------------------------------------------------------
# TerminalMonitor Core Engine (Class API)
# ---------------------------------------------------------------------------

class TerminalMonitor:
    """Main monitor engine supporting event callbacks and step/run lifecycle."""

    def __init__(
        self,
        config: MonitorConfig,
        backend: BaseTerminalBackend | None = None,
        profile: AgentProfile | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or get_backend(config.backend)
        self.profile = profile or get_profile(config.profile or config.process, config.custom_profiles)

        # Merge profile process if config process was default
        if config.process in ("opencode", "agent", "") and self.profile.process != "generic":
            self.config.process = self.profile.process

        # Merge unsafe phrases
        if config.unsafe_phrases:
            self.profile.unsafe_phrases = list(set(self.profile.unsafe_phrases + config.unsafe_phrases))

        # Setup state paths
        self.state_dir = config.state_dir
        os.makedirs(self.state_dir, exist_ok=True)
        self.log_path = os.path.join(self.state_dir, "monitor.log")
        self.attention_path = os.path.join(self.state_dir, "attention.txt")
        self.answer_path = os.path.join(self.state_dir, "answer.txt")
        self.stop_path = os.path.join(self.state_dir, "stop")

        # Internal state tracking
        self.last_digest = ""
        self.last_change = time.monotonic()
        self.last_send = 0.0
        self.last_seen = time.monotonic()
        self.sends = 0
        self.current_state = "unknown"

        # Event callbacks
        self.on_state_change: Callable[[str, str], None] | None = None
        self.on_send: Callable[[str, str, bool], None] | None = None
        self.on_attention: Callable[[str, str], None] | None = None
        self.on_tick: Callable[[str, int], None] | None = None

    def log(self, message: str) -> None:
        append_log(self.log_path, message)

    def inspect(self) -> dict[str, Any]:
        """Perform a single read-only inspection of the terminal tab and process."""
        pids = self.backend.get_pids(self.config.process)
        tab = self.backend.get_tab(self.config.process, self.config.title)
        if not tab.get("ok"):
            return {
                "ok": False,
                "error": tab.get("error", "unknown error"),
                "pids": pids,
                "state": "missing",
            }

        history = str(tab.get("hist", ""))
        snapshot = normalize_snapshot(history)
        state = classify_state(history, self.profile)
        return {
            "ok": True,
            "pids": pids,
            "state": state,
            "snapshot": snapshot,
            "tab": tab,
        }

    def step(self) -> tuple[int | None, str]:
        """Perform a single monitor iteration.

        Returns (exit_code, status_message).
        If exit_code is None, the monitor should continue running.
        """
        if os.path.exists(self.stop_path):
            return 0, "CANCELLED"

        pids = self.backend.get_pids(self.config.process)
        if pids:
            self.last_seen = time.monotonic()
        elif time.monotonic() - self.last_seen >= self.config.gone_seconds:
            return 0, "PROCESS_GONE"

        tab = self.backend.get_tab(self.config.process, self.config.title)
        if not tab.get("ok"):
            if self.config.once:
                return 2, f"MISSING: {tab.get('error')}"
            return None, "TAB_MISSING"

        history = str(tab.get("hist", ""))
        snapshot = normalize_snapshot(history)
        digest = hashlib.sha256(snapshot.encode("utf-8", "replace")).hexdigest()[:16]
        state = classify_state(history, self.profile)

        if state != self.current_state:
            if self.on_state_change:
                self.on_state_change(self.current_state, state)
            self.current_state = state

        if self.config.once:
            return 0, f"STATE={state} PID_COUNT={len(pids)}"

        if digest != self.last_digest:
            self.last_digest = digest
            self.last_change = time.monotonic()

        stable_for = time.monotonic() - self.last_change
        threshold = 4.0 if state in ("permission", "question") else self.config.idle_seconds
        if self.config.idle_seconds == 0.0:
            threshold = 0.0

        if state == "thinking" or stable_for < threshold:
            if self.on_tick:
                self.on_tick(state, len(pids))
            return None, f"WAITING state={state} stable_for={stable_for:.1f}"

        if time.monotonic() - self.last_send < self.config.cooldown_seconds:
            return None, "COOLDOWN"

        manual_answer = consume_manual_answer(self.answer_path)
        payload: str | None = manual_answer or self.config.continue_text
        reason = "idle"

        if manual_answer:
            reason = "manual"
        elif state == "permission":
            payload = self.profile.auto_permission_payload if self.config.auto_allow_permissions else None
            reason = "permission"
        elif state == "question":
            payload = decide_question(history, self.profile)
            reason = "question"

        if payload is None:
            with open(self.attention_path, "w", encoding="utf-8") as handle:
                handle.write(snapshot + "\n")
            if self.on_attention:
                self.on_attention(reason, snapshot)
            self.log(f"PAUSE kind={reason} hash={digest}")
            return 3, f"ATTENTION_REQUIRED kind={reason} file={self.attention_path}"

        if self.config.dry_run:
            return 0, f"DRY_RUN kind={reason} payload={payload or '<enter>'}"

        ok, detail = self.backend.send(self.config.process, self.config.title, payload)
        self.sends += 1
        self.last_send = time.monotonic()
        self.log(f"SEND kind={reason} n={self.sends} ok={ok} detail={detail}")

        if self.on_send:
            self.on_send(reason, payload, ok)

        if not ok:
            return 1, f"SEND_FAILED kind={reason} n={self.sends}"

        if self.sends >= self.config.max_sends:
            return 0, "MAX_SENDS_REACHED"

        return None, f"SENT kind={reason} n={self.sends}"

    def run(self) -> int:
        """Run monitor loop until completion or exit condition."""
        self.log(f"START process={self.config.process} profile={self.profile.name} dry_run={self.config.dry_run}")
        while True:
            code, msg = self.step()
            if code is not None:
                print(msg, flush=True)
                return code
            if "SENT" in msg or "SEND_FAILED" in msg:
                print(msg, flush=True)
            time.sleep(self.config.poll_seconds)


# ---------------------------------------------------------------------------
# CLI Argument Parsing and Main Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser with profile, backend, and config support."""
    parser = argparse.ArgumentParser(
        description="Monitor any AI CLI agent in macOS Terminal.app, iTerm2, or tmux and send safe nudges."
    )
    parser.add_argument("--profile", help="agent profile (e.g. claude, opencode, aider, goose, generic)")
    parser.add_argument("--process", default=None, help="exact process name to monitor (defaults to profile process)")
    parser.add_argument("--title", help="optional substring of the Terminal tab title or window name")
    parser.add_argument("--backend", default="auto", choices=["auto", "terminal", "iterm2", "tmux"], help="terminal backend to use")
    parser.add_argument("--config", help="path to custom JSON or TOML config file")
    parser.add_argument("--project-dir", default=".", help="project directory containing .terminal-monitor config")

    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--continue-text", help="text sent when the CLI becomes idle")
    source.add_argument("--continue-file", help="UTF-8 file containing continuation text")

    parser.add_argument("--poll-seconds", type=float, default=None, help="polling interval in seconds (default: 3.0)")
    parser.add_argument("--idle-seconds", type=float, default=None, help="seconds of inactivity to declare idle (default: 15.0)")
    parser.add_argument("--cooldown-seconds", type=float, default=None, help="seconds between nudges (default: 20.0)")
    parser.add_argument("--gone-seconds", type=float, default=None, help="seconds before missing process exits (default: 25.0)")
    parser.add_argument("--max-sends", type=int, default=None, help="maximum number of nudges to send (default: 100)")
    parser.add_argument("--auto-allow-permissions", action="store_true", default=None, help="auto-approve permission prompts")
    parser.add_argument("--once", action="store_true", help="inspect once and exit without sending")
    parser.add_argument("--dry-run", action="store_true", help="monitor and print decisions without sending")
    parser.add_argument("--state-dir", default=None, help="state and logs directory (default: /tmp/terminal-monitor)")
    parser.add_argument("--add-unsafe-phrase", action="append", default=[], help="additional unsafe phrase to block")
    parser.add_argument("--list-profiles", action="store_true", help="list available built-in and configured profiles")
    parser.add_argument("--init-config", choices=["json", "toml"], help="generate a starter configuration file")

    return parser


def config_from_args(args: argparse.Namespace) -> MonitorConfig:
    """Build MonitorConfig merging defaults, discovered config file, and CLI flags."""
    # Handle list profiles and init config special flags
    if getattr(args, "list_profiles", False) or getattr(args, "init_config", None):
        return MonitorConfig()

    # Discover and load config file if present
    file_data: dict[str, Any] = {}
    config_path = args.config
    if config_path:
        file_data = load_config_file(config_path)
    else:
        discovered = discover_config_file(getattr(args, "project_dir", "."))
        if discovered:
            file_data = load_config_file(discovered)

    # Determine profile
    profile_name = args.profile or file_data.get("profile") or "opencode"
    custom_profiles = file_data.get("custom_profiles", {})
    profile_obj = get_profile(profile_name, custom_profiles)

    # Determine process name
    process_name = args.process or file_data.get("process") or profile_obj.process or "opencode"
    process_name = validate_process_name(process_name)

    # Determine continue text
    continue_text = ""
    if args.continue_file:
        with open(args.continue_file, encoding="utf-8") as handle:
            continue_text = handle.read().strip()
    elif args.continue_text:
        continue_text = args.continue_text.strip()
    elif file_data.get("continue_file"):
        with open(file_data["continue_file"], encoding="utf-8") as handle:
            continue_text = handle.read().strip()
    elif file_data.get("continue_text"):
        continue_text = str(file_data["continue_text"]).strip()
    elif profile_obj.default_continue_text:
        continue_text = profile_obj.default_continue_text.strip()

    # For commands like --once, empty continue_text is permissible
    if not continue_text and not args.once and not getattr(args, "dry_run", False):
        raise ValueError("continuation text cannot be empty (provide --continue-text or --continue-file)")

    # Timing parameters
    poll_seconds = args.poll_seconds if args.poll_seconds is not None else float(file_data.get("poll_seconds", 3.0))
    idle_seconds = args.idle_seconds if args.idle_seconds is not None else float(file_data.get("idle_seconds", 15.0))
    cooldown_seconds = args.cooldown_seconds if args.cooldown_seconds is not None else float(file_data.get("cooldown_seconds", 20.0))
    gone_seconds = args.gone_seconds if args.gone_seconds is not None else float(file_data.get("gone_seconds", 25.0))
    max_sends = args.max_sends if args.max_sends is not None else int(file_data.get("max_sends", 100))

    for name, val in [
        ("poll_seconds", poll_seconds),
        ("idle_seconds", idle_seconds),
        ("cooldown_seconds", cooldown_seconds),
        ("gone_seconds", gone_seconds),
    ]:
        if val <= 0:
            raise ValueError(f"{name.replace('_', '-')} must be greater than zero")
    if max_sends < 1:
        raise ValueError("max-sends must be at least 1")

    # Booleans & paths
    auto_allow = args.auto_allow_permissions if args.auto_allow_permissions is not None else bool(file_data.get("auto_allow_permissions", False))
    state_dir = args.state_dir or file_data.get("state_dir") or "/tmp/terminal-monitor"
    backend = args.backend if args.backend != "auto" else file_data.get("backend", "auto")
    title = args.title if args.title is not None else file_data.get("title")

    # Unsafe phrases
    unsafe_phrases = list(UNSAFE_PHRASES)
    if "unsafe_phrases" in file_data and isinstance(file_data["unsafe_phrases"], list):
        unsafe_phrases = list(file_data["unsafe_phrases"])
    if args.add_unsafe_phrase:
        unsafe_phrases.extend(args.add_unsafe_phrase)

    return MonitorConfig(
        process=process_name,
        profile=profile_name,
        title=title,
        continue_text=continue_text,
        poll_seconds=poll_seconds,
        idle_seconds=idle_seconds,
        cooldown_seconds=cooldown_seconds,
        gone_seconds=gone_seconds,
        max_sends=max_sends,
        auto_allow_permissions=auto_allow,
        once=bool(args.once),
        dry_run=bool(args.dry_run),
        state_dir=state_dir,
        backend=backend,
        project_dir=getattr(args, "project_dir", "."),
        unsafe_phrases=unsafe_phrases,
        custom_profiles=custom_profiles,
    )


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_profiles:
        custom_profiles: dict[str, Any] = {}
        cfg_file = discover_config_file(getattr(args, "project_dir", "."))
        if cfg_file:
            try:
                custom_profiles = load_config_file(cfg_file).get("custom_profiles", {})
            except Exception:
                pass
        profiles = list_profiles(custom_profiles)
        print("Available Agent Profiles:")
        for name, desc in sorted(profiles.items()):
            print(f"  • {name:<12} : {desc}")
        return 0

    if args.init_config:
        try:
            content = generate_starter_config(args.init_config)
            print(content, end="")
            return 0
        except Exception as exc:
            parser.error(str(exc))

    try:
        config = config_from_args(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    monitor = TerminalMonitor(config)
    return monitor.run()


if __name__ == "__main__":
    sys.exit(main())
