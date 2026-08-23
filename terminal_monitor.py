#!/usr/bin/env python3
"""Monitor and safely nudge any AI CLI agent running in macOS Terminal.app, iTerm2, or tmux.

Generic and extensible across any agent (Claude Code, OpenCode, Aider, Goose, etc.)
and any project via configuration files, profiles, and customizable rules.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

SPECIAL_KEY_CODES: dict[str, int] = {
    "tab": 9,
    "\t": 9,
    "enter": 13,
    "return": 13,
    "\r": 13,
    "\n": 10,
    "esc": 27,
    "escape": 27,
    "\x1b": 27,
    "ctrl+c": 3,
    "ctrl_c": 3,
    "\x03": 3,
    "ctrl+p": 16,
    "ctrl_p": 16,
    "\x10": 16,
    "ctrl+d": 4,
    "ctrl_d": 4,
    "\x04": 4,
    "backspace": 127,
    "delete": 127,
}


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


def is_table_or_box_line(line: str) -> bool:
    """Check if a line is part of a markdown or unicode box/table."""
    stripped = line.strip()
    if not stripped:
        return False
    if re.search(r"[┌├└┬┴┼─━═]", stripped):
        return True
    if stripped.count("│") >= 2 or stripped.count("|") >= 2:
        return True
    return bool(re.match(r"^[\s|+_=-]+$", stripped))


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
    mode_patterns: dict[str, str] = field(default_factory=dict)
    plan_ready_patterns: list[str] = field(default_factory=list)
    mode_switch_key: str = "tab"
    completion_patterns: list[str] = field(default_factory=list)

    def matches_thinking(self, history_tail: str) -> bool:
        return any(match_pattern(pat, history_tail) for pat in self.thinking_patterns)

    def matches_permission(self, history_tail: str) -> bool:
        return any(match_pattern(pat, history_tail) for pat in self.permission_patterns)

    def matches_question(self, history_tail: str) -> bool:
        if any(match_pattern(pat, history_tail) for pat in self.question_indicators):
            return True
        options = self.extract_options(history_tail)
        if len(options) >= 2:
            prompt_cue = any(
                re.search(pat, history_tail, re.IGNORECASE)
                for pat in (r"\?", r"question", r"select", r"choose", r"option", r"recommended", r"⇆", r"\(1-", r"\[y/n\]", r"enter confirm")
            )
            if prompt_cue:
                return True
        return False

    def matches_completion(self, history_tail: str) -> bool:
        if not self.completion_patterns:
            return False
        return any(match_pattern(pat, history_tail) for pat in self.completion_patterns)

    def detect_mode(self, history_tail: str) -> str | None:
        """Detect the active TUI mode from history tail if patterns are configured."""
        if not self.mode_patterns:
            return None
        for mode, pattern in self.mode_patterns.items():
            if re.search(pattern, history_tail, re.IGNORECASE | re.MULTILINE):
                return mode
        return None

    def is_plan_ready(self, history_tail: str) -> bool:
        """Detect if the agent has finished planning and is waiting for approval."""
        if not self.plan_ready_patterns:
            return False
        return any(match_pattern(pat, history_tail) for pat in self.plan_ready_patterns)

    def extract_options(self, history_tail: str) -> list[tuple[str, bool]]:
        """Extract selectable options and their recommendation status from history tail."""
        options: list[tuple[str, bool]] = []
        in_code_block = False
        for line in history_tail.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue
            if is_table_or_box_line(line):
                continue

            is_option = any(match_pattern(pat, line) for pat in self.option_patterns)
            if not is_option and re.search(r"recommended", line, re.IGNORECASE):
                is_option = True

            if is_option:
                clean = clean_option(line)
                if clean and len(clean) >= 2:
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
            r"~ (writing|updating|reading|running)",
        ],
        permission_patterns=[
            r"allow.*deny",
            "allow once",
            "allow always",
            "permission required",
            "do you want to run",
        ],
        question_indicators=[
            "(recommended)",
            r"\b(choose|select|which option|pick one|what should)\b",
            "⇆ tab",
            "⇆ select",
            "enter confirm",
        ],
        option_patterns=[
            r"^\s*[●○◉❯]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="",
        mode_patterns={
            "plan": r"Plan\s*·\s*\w+",
            "build": r"Build\s*·\s*\w+",
        },
        plan_ready_patterns=[
            "plano pronto",
            "plan ready",
            "plan complete",
            "aprove para eu sair do modo plano",
            "ready to build",
        ],
        mode_switch_key="tab",
        completion_patterns=[
            "100% concluído",
            "todas as tarefas estão concluídas",
            "todas as 20 tasks estão concluídas",
            "não há próxima task",
            "não há trabalho restante no plano",
            "all tasks completed",
            "plan is complete",
            "todos os prs mergeados",
        ],
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
            r"\[yes\]:",
            r"\(y\)es/\(n\)o",
            r"\bquestion\b",
        ],
        option_patterns=[
            r"^\s*[●○◉❯>]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="y",
        completion_patterns=[
            "all tasks complete",
            "task completed successfully",
            "done with all tasks",
        ],
    ),
    "claude-code": AgentProfile(
        name="claude-code",
        process="claude",
        description="Alias for Anthropic Claude Code CLI (derived from the claude profile)",
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
            "working...",
            "processing...",
            "generating...",
            "please wait...",
            "esc to cancel",
            "esc interrupt",
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


# claude-code is a full alias of the claude profile, derived to avoid duplication.
BUILTIN_PROFILES["claude-code"] = replace(
    BUILTIN_PROFILES["claude"],
    name="claude-code",
    description="Alias for Anthropic Claude Code CLI",
)


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
                mode_patterns=val.get("mode_patterns", {}),
                plan_ready_patterns=val.get("plan_ready_patterns", []),
                mode_switch_key=val.get("mode_switch_key", "tab"),
                completion_patterns=val.get("completion_patterns", []),
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
        mode_patterns=dict(generic.mode_patterns),
        plan_ready_patterns=list(generic.plan_ready_patterns),
        mode_switch_key=generic.mode_switch_key,
        completion_patterns=list(generic.completion_patterns),
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
    # Supervision & Autonomous Extensions
    supervise: bool = False
    auto_switch_modes: bool = True
    smart_nudges: bool = True
    completion_check: bool = True
    status_json_path: str | None = None


# ---------------------------------------------------------------------------
# Terminal Backends
# ---------------------------------------------------------------------------

def validate_process_name(process: str) -> str:
    """Ensure process name contains only safe alphanumeric/dash/underscore characters."""
    clean = process.strip()
    if not clean or not re.match(r"^[A-Za-z0-9_.-]+$", clean):
        raise ValueError(f"Invalid process name: {process!r}")
    return clean


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


OSASCRIPT_TIMEOUT_SECONDS = 15.0
COMMAND_TIMEOUT_SECONDS = 30.0


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
        return 1, f"osascript timed out after {int(OSASCRIPT_TIMEOUT_SECONDS)}s"
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


def parse_tab_output(raw: str) -> dict[str, str | bool]:
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
    return data


class BaseTerminalBackend:
    """Abstract base class for terminal interaction backends."""

    def name(self) -> str:
        raise NotImplementedError

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        raise NotImplementedError

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

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        process = validate_process_name(process)
        process_literal = applescript_escape(process)
        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
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
        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
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

        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
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

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
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

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
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
# Git Context Engine & Smart Nudges
# ---------------------------------------------------------------------------

@dataclass
class GitStatus:
    """Git workspace status snapshot."""

    is_repo: bool = False
    branch: str = ""
    dirty: bool = False
    modified_count: int = 0
    untracked_count: int = 0
    commits_ahead: int = 0
    open_prs_count: int = 0
    last_commit: str = ""
    summary: str = ""


GIT_STATUS_TTL_SECONDS = 30.0
_GIT_STATUS_CACHE: dict[str, tuple[float, GitStatus]] = {}


def get_git_status(repo_dir: str = ".", ttl_seconds: float = GIT_STATUS_TTL_SECONDS) -> GitStatus:
    """Cached wrapper around :func:`_get_git_status_uncached` with a TTL per repository."""
    try:
        key = str(Path(repo_dir).resolve())
    except OSError:
        key = repo_dir
    now = time.monotonic()
    cached = _GIT_STATUS_CACHE.get(key)
    if cached is not None and now - cached[0] < ttl_seconds:
        return cached[1]
    status = _get_git_status_uncached(repo_dir)
    _GIT_STATUS_CACHE[key] = (now, status)
    return status


def _get_git_status_uncached(repo_dir: str = ".") -> GitStatus:
    """Inspect git repository status safely without mutating workspace."""
    try:
        code, out, _ = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
        if code != 0 or "true" not in out:
            return GitStatus(is_repo=False)

        branch_code, branch_out, _ = run_command(["git", "branch", "--show-current"], cwd=repo_dir)
        branch = branch_out.strip() if branch_code == 0 else ""

        _status_code, status_out, _ = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
        status_lines = [line for line in status_out.splitlines() if line.strip() and not line.strip().endswith(".DS_Store")]
        dirty = len(status_lines) > 0
        untracked = sum(1 for line in status_lines if line.startswith("??"))
        modified = len(status_lines) - untracked

        log_code, log_out, _ = run_command(["git", "log", "-n", "1", "--oneline"], cwd=repo_dir)
        last_commit = log_out.strip() if log_code == 0 else ""

        open_prs = 0
        if shutil.which("gh"):
            gh_code, gh_out, _ = run_command(["gh", "pr", "list", "--state", "open", "--json", "number"], cwd=repo_dir)
            if gh_code == 0:
                with contextlib.suppress(Exception):
                    open_prs = len(json.loads(gh_out))

        summary = f"branch={branch} dirty={dirty} mod={modified} untracked={untracked} prs={open_prs}"
        return GitStatus(
            is_repo=True,
            branch=branch,
            dirty=dirty,
            modified_count=modified,
            untracked_count=untracked,
            commits_ahead=0,
            open_prs_count=open_prs,
            last_commit=last_commit,
            summary=summary,
        )
    except Exception:
        return GitStatus(is_repo=False)


def generate_smart_nudge(git_status: GitStatus, default_nudge: str = "") -> str:
    """Generate a context-aware nudge based on git repository status."""
    if not git_status.is_repo:
        return default_nudge or "Continue with the next task according to the plan."

    if git_status.dirty:
        return (
            f"You have uncommitted changes on branch '{git_status.branch}'. "
            "Execute targeted tests for the current task, verify everything passes, and commit your changes."
        )

    if git_status.open_prs_count > 0:
        return "Check the status of open Pull Requests and CI checks, and proceed with merging once all checks pass."

    if git_status.branch not in ("main", "master", ""):
        return (
            f"Your working tree on branch '{git_status.branch}' is clean. "
            "Run full verification, push your branch, create a Pull Request and merge it."
        )

    return default_nudge or "Continue with the next task according to the plan."


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
    """Classify the current terminal state (permission, question, completed, thinking, idle).

    Actionable states (permission/question/completed) take precedence over
    "thinking" because agents often keep spinner hints like "esc to cancel"
    visible while a permission prompt is on screen.
    """
    prof = profile or BUILTIN_PROFILES["opencode"]
    tail = "\n".join(history.splitlines()[-50:])

    if prof.matches_permission(tail):
        return "permission"
    if prof.matches_question(tail):
        return "question"
    if prof.matches_completion(tail):
        return "completed"
    if prof.matches_thinking(tail):
        return "thinking"
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
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
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
    except Exception as json_err:
        if tomllib is not None:
            try:
                return tomllib.loads(text)
            except Exception:
                pass
        raise ValueError(f"Could not parse configuration file as JSON or TOML: {file_path}") from json_err


def generate_starter_config(format_type: str = "json") -> str:
    """Generate a starter configuration template in JSON or TOML."""
    if format_type.lower() == "json":
        return json.dumps(
            {
                "profile": "opencode",
                "process": "opencode",
                "continue_text": "Continue with the next task according to the plan.",
                "poll_seconds": 3.0,
                "idle_seconds": 15.0,
                "cooldown_seconds": 20.0,
                "gone_seconds": 25.0,
                "max_sends": 100,
                "auto_allow_permissions": True,
                "supervise": True,
                "smart_nudges": True,
                "auto_switch_modes": True,
                "completion_check": True,
                "unsafe_phrases": list(UNSAFE_PHRASES),
                "custom_profiles": {
                    "my-agent": {
                        "process": "myagent",
                        "description": "Custom agent CLI configuration",
                        "thinking_patterns": ["agent is thinking...", "processing..."],
                        "permission_patterns": ["do you authorize this action?"],
                        "auto_permission_payload": "y",
                        "mode_patterns": {"plan": "Plan Mode", "build": "Build Mode"},
                        "completion_patterns": ["all tasks complete"],
                    }
                },
            },
            indent=2,
        )
    elif format_type.lower() == "toml":
        return """profile = "opencode"
process = "opencode"
continue_text = "Continue with the next task according to the plan."
poll_seconds = 3.0
idle_seconds = 15.0
cooldown_seconds = 20.0
max_sends = 100
auto_allow_permissions = true
supervise = true
smart_nudges = true
auto_switch_modes = true
completion_check = true

unsafe_phrases = ["bypass", "delete", "rm -rf", "reset --hard"]

[custom_profiles.my-agent]
process = "myagent"
description = "Custom agent CLI configuration"
thinking_patterns = ["agent is thinking...", "processing..."]
permission_patterns = ["do you authorize this action?"]
auto_permission_payload = "y"
completion_patterns = ["all tasks complete"]
"""
    else:
        raise ValueError(f"Unsupported format: {format_type}. Use 'json' or 'toml'.")


# ---------------------------------------------------------------------------
# TerminalMonitor Core Engine (Class API)
# ---------------------------------------------------------------------------

class TerminalMonitor:
    """Main monitor engine supporting event callbacks, mode management, and step/run lifecycle."""

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
        self.status_json_path = config.status_json_path

        # Internal state tracking
        self.last_digest = ""
        self.last_change = time.monotonic()
        self.last_seen = time.monotonic()
        self.last_send = 0.0
        self.sends = 0
        self.current_state = "unknown"
        self.current_mode: str | None = None

        # Callbacks
        self.on_state_change: Callable[[str, str], None] | None = None
        self.on_mode_change: Callable[[str | None, str | None], None] | None = None
        self.on_send: Callable[[str, str, bool], None] | None = None
        self.on_attention: Callable[[str, str], None] | None = None
        self.on_complete: Callable[[str], None] | None = None
        self.on_tick: Callable[[str, int], None] | None = None

    def _effective_threshold(self, state: str) -> float:
        """Idle seconds to wait before acting; actionable prompts act faster."""
        if self.config.idle_seconds == 0.0:
            return 0.0
        return 4.0 if state in ("permission", "question") else self.config.idle_seconds

    def log(self, message: str) -> None:
        append_log(self.log_path, message)

    def export_status_json(self, pids: list[int], state: str, extra: dict[str, Any] | None = None) -> None:
        """Export live structured status for IDEs, dashboards, or subagents."""
        if not self.status_json_path:
            return
        git_status = get_git_status(self.config.project_dir)
        data: dict[str, Any] = {
            "running": True,
            "pids": pids,
            "process": self.config.process,
            "profile": self.profile.name,
            "state": state,
            "mode": self.current_mode,
            "sends": self.sends,
            "stable_seconds": round(time.monotonic() - self.last_change, 1),
            "git": {
                "branch": git_status.branch,
                "dirty": git_status.dirty,
                "modified": git_status.modified_count,
                "untracked": git_status.untracked_count,
                "open_prs": git_status.open_prs_count,
                "last_commit": git_status.last_commit,
            },
            "timestamp": now_iso(),
        }
        if extra:
            data.update(extra)
        try:
            target_path = Path(self.status_json_path).resolve()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def inspect(self) -> dict[str, Any]:
        """Query current tab state and return structured status snapshot."""
        pids = self.backend.get_pids(self.config.process)
        tab = self.backend.get_tab(self.config.process, self.config.title)
        if not tab.get("ok"):
            return {
                "ok": False,
                "error": tab.get("error"),
                "pids": pids,
                "state": "missing",
            }

        history = str(tab.get("hist", ""))
        snapshot = normalize_snapshot(history)
        state = classify_state(history, self.profile)
        mode = self.profile.detect_mode(history)
        return {
            "ok": True,
            "pids": pids,
            "state": state,
            "mode": mode,
            "snapshot": snapshot,
            "tab": tab,
        }

    def step(self) -> tuple[int | None, str]:
        """Perform a single monitor iteration.

        Returns (exit_code, status_message).
        If exit_code is None, the monitor should continue running.
        """
        if os.path.exists(self.stop_path):
            self.export_status_json([], "cancelled", {"running": False})
            return 0, "CANCELLED"

        pids = self.backend.get_pids(self.config.process)
        if pids:
            self.last_seen = time.monotonic()
        elif time.monotonic() - self.last_seen >= self.config.gone_seconds:
            self.export_status_json([], "process_gone", {"running": False})
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
        mode = self.profile.detect_mode(history)

        if mode != self.current_mode:
            if self.on_mode_change:
                self.on_mode_change(self.current_mode, mode)
            self.current_mode = mode

        if state != self.current_state:
            if self.on_state_change:
                self.on_state_change(self.current_state, state)
            self.current_state = state

        self.export_status_json(pids, state)

        if self.config.once:
            return 0, f"STATE={state} MODE={mode} PID_COUNT={len(pids)}"

        if digest != self.last_digest:
            self.last_digest = digest
            self.last_change = time.monotonic()

        stable_for = time.monotonic() - self.last_change

        # Handle Completion State
        if state == "completed" and self.config.completion_check:
            self.log("SUCCESS: Completion indicators detected. Work complete.")
            if self.on_complete:
                self.on_complete(snapshot)
            self.export_status_json(pids, "completed", {"done": True})
            return 0, "COMPLETED"

        threshold = self._effective_threshold(state)

        if state == "thinking" or stable_for < threshold:
            if self.on_tick:
                self.on_tick(state, len(pids))
            return None, f"WAITING state={state} stable_for={stable_for:.1f}"

        if time.monotonic() - self.last_send < self.config.cooldown_seconds:
            return None, "COOLDOWN"

        mode_threshold = self._effective_threshold("permission")
        # Handle Plan Mode Auto-Transition
        if self.config.auto_switch_modes and mode == "plan" and self.profile.is_plan_ready(history) and stable_for >= mode_threshold:
            self.log("MODE: Plan completed. Auto-switching mode via switch key.")
            ok, detail = self.backend.send_key(self.config.process, self.config.title, self.profile.mode_switch_key)
            self.last_send = time.monotonic()
            self.last_change = time.monotonic()
            if self.config.continue_text:
                time.sleep(0.5)
                self.backend.send(self.config.process, self.config.title, self.config.continue_text)
            return None, "MODE_SWITCH_SENT"

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
        elif state == "idle" and self.config.smart_nudges:
            git_info = get_git_status(self.config.project_dir)
            payload = generate_smart_nudge(git_info, self.config.continue_text)
            reason = "smart_nudge"

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
        """Run monitor loop continuously until exit condition is met."""
        self.log(f"START process={self.config.process} profile={self.profile.name} backend={self.backend.name()}")
        try:
            while True:
                code, msg = self.step()
                if code is not None:
                    self.log(f"EXIT code={code} msg={msg}")
                    return code
                time.sleep(self.config.poll_seconds)
        except KeyboardInterrupt:
            self.log("EXIT code=130 msg=INTERRUPTED")
            self.export_status_json([], "interrupted", {"running": False})
            return 130


# ---------------------------------------------------------------------------
# CLI Parser and Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build comprehensive CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="terminal_monitor",
        description="Monitor and safely nudge AI CLI coding agents running in Terminal.app, iTerm2, or tmux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # init config subcommand
    init_parser = subparsers.add_parser("init", help="Generate a starter configuration file")
    init_parser.add_argument("--format", choices=["json", "toml"], default="json", help="Configuration format (default: json)")
    init_parser.add_argument("-o", "--output", help="Output file path (default: .terminal-monitor.<format>)")

    # list profiles subcommand
    subparsers.add_parser("profiles", help="List built-in and discovered agent profiles")

    # supervise subcommand
    supervise_parser = subparsers.add_parser("supervise", help="Run autonomous supervision daemon")
    _add_monitor_args(supervise_parser)
    supervise_parser.add_argument("--status-json", dest="status_json_path", help="Path to write real-time status JSON")

    # Main monitor arguments
    _add_monitor_args(parser)
    parser.add_argument("--status-json", dest="status_json_path", help="Path to write real-time status JSON")

    return parser


def _add_monitor_args(parser: argparse.ArgumentParser) -> None:
    """Add standard monitor flags to a parser with None defaults so config files take effect."""
    parser.add_argument("--process", "-p", default=None, help="Agent process name to track (default: opencode)")
    parser.add_argument("--profile", default=None, help="Agent profile to use (e.g. claude, opencode, aider, goose)")
    parser.add_argument("--title", "-t", default=None, help="Window title substring filter")
    parser.add_argument("--continue-text", "-c", default=None, help="Text sent on idle or nudge")
    parser.add_argument("--continue-file", "-f", default=None, help="Path to file whose content is sent on idle")
    parser.add_argument("--poll-seconds", type=float, default=None, help="Loop interval (default: 3.0s)")
    parser.add_argument("--idle-seconds", type=float, default=None, help="Seconds before idle trigger (default: 15.0s)")
    parser.add_argument("--cooldown-seconds", type=float, default=None, help="Cooldown after sending (default: 20.0s)")
    parser.add_argument("--gone-seconds", type=float, default=None, help="Seconds before process gone (default: 25.0s)")
    parser.add_argument("--max-sends", type=int, default=None, help="Maximum sends before exit (default: 100)")
    parser.add_argument("--auto-allow-permissions", "-a", action="store_true", default=None, help="Auto-allow safe permission prompts")
    parser.add_argument("--supervise", "-S", action="store_true", default=None, help="Enable autonomous supervisor mode")
    parser.add_argument("--no-smart-nudges", action="store_true", help="Disable git-aware context smart nudges")
    parser.add_argument("--no-mode-switch", action="store_true", help="Disable automatic Plan->Build mode switching")
    parser.add_argument("--no-completion-check", action="store_true", help="Disable completion state auto-detection")
    parser.add_argument("--backend", "-b", choices=["auto", "terminal", "iterm2", "tmux"], default=None, help="Terminal backend")
    parser.add_argument("--project-dir", "-d", default=None, help="Project directory for config discovery and git status")
    parser.add_argument("--config", default=None, help="Explicit configuration file path")
    parser.add_argument("--state-dir", default=None, help="Directory for state/logs (default: /tmp/terminal-monitor)")
    parser.add_argument("--unsafe-phrase", action="append", dest="unsafe_phrases", help="Add custom unsafe phrases")
    parser.add_argument("--once", action="store_true", default=False, help="Inspect status once and exit")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Simulate actions without sending keystrokes")


def config_from_args(args: argparse.Namespace) -> MonitorConfig:
    """Build MonitorConfig merging defaults, discovered config file, and CLI flags."""
    project_dir = getattr(args, "project_dir", None) or "."
    file_cfg: dict[str, Any] = {}
    if getattr(args, "config", None):
        file_cfg = load_config_file(args.config)
    elif project_dir:
        discovered = discover_config_file(project_dir)
        if discovered:
            file_cfg = load_config_file(discovered)

    cli_unsafe = getattr(args, "unsafe_phrases", None) or []
    file_unsafe = file_cfg.get("unsafe_phrases", list(UNSAFE_PHRASES))
    merged_unsafe = list(dict.fromkeys(list(file_unsafe) + list(cli_unsafe)))

    continue_text = args.continue_text if getattr(args, "continue_text", None) is not None else file_cfg.get("continue_text", "")
    if getattr(args, "continue_file", None):
        continue_path = Path(args.continue_file).resolve()
        if continue_path.is_file():
            continue_text = continue_path.read_text(encoding="utf-8").strip()

    is_supervise = bool(getattr(args, "supervise", False) or getattr(args, "command", "") == "supervise" or file_cfg.get("supervise", False))
    auto_allow = bool(args.auto_allow_permissions if getattr(args, "auto_allow_permissions", None) is not None else (is_supervise or file_cfg.get("auto_allow_permissions", False)))
    smart_nudges = not getattr(args, "no_smart_nudges", False) and bool(is_supervise or file_cfg.get("smart_nudges", True))
    auto_switch = not getattr(args, "no_mode_switch", False) and bool(is_supervise or file_cfg.get("auto_switch_modes", True))
    completion_check = not getattr(args, "no_completion_check", False) and bool(is_supervise or file_cfg.get("completion_check", True))

    process = args.process or file_cfg.get("process", "opencode")
    profile = args.profile or file_cfg.get("profile", process)

    def _val(arg_val: Any, cfg_key: str, default_val: Any) -> Any:
        if arg_val is not None:
            return arg_val
        return file_cfg.get(cfg_key, default_val)

    return MonitorConfig(
        process=process,
        profile=profile,
        title=_val(getattr(args, "title", None), "title", None),
        continue_text=continue_text,
        continue_file=getattr(args, "continue_file", None),
        poll_seconds=float(_val(getattr(args, "poll_seconds", None), "poll_seconds", 3.0)),
        idle_seconds=float(_val(getattr(args, "idle_seconds", None), "idle_seconds", 15.0)),
        cooldown_seconds=float(_val(getattr(args, "cooldown_seconds", None), "cooldown_seconds", 20.0)),
        gone_seconds=float(_val(getattr(args, "gone_seconds", None), "gone_seconds", 25.0)),
        max_sends=int(_val(getattr(args, "max_sends", None), "max_sends", 100)),
        auto_allow_permissions=auto_allow,
        once=bool(getattr(args, "once", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        state_dir=str(_val(getattr(args, "state_dir", None), "state_dir", "/tmp/terminal-monitor")),
        backend=str(_val(getattr(args, "backend", None), "backend", "auto")),
        project_dir=str(project_dir),
        unsafe_phrases=merged_unsafe,
        custom_profiles=file_cfg.get("custom_profiles", {}),
        supervise=is_supervise,
        auto_switch_modes=auto_switch,
        smart_nudges=smart_nudges,
        completion_check=completion_check,
        status_json_path=getattr(args, "status_json_path", None) or file_cfg.get("status_json_path"),
    )


def main() -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    # Handle init subcommand
    if args.command == "init":
        fmt = getattr(args, "format", "json")
        out_path = getattr(args, "output", None) or f".terminal-monitor.{fmt}"
        content = generate_starter_config(fmt)
        Path(out_path).write_text(content + "\n", encoding="utf-8")
        print(f"Created configuration template: {out_path}")
        return 0

    # Handle profiles subcommand
    if args.command == "profiles":
        print("Available Agent Profiles:")
        for name, desc in list_profiles().items():
            print(f"  • {name:<12} - {desc}")
        return 0

    config = config_from_args(args)
    monitor = TerminalMonitor(config)

    if config.once:
        inspected = monitor.inspect()
        print(json.dumps(inspected, indent=2))
        return 0 if inspected.get("ok") else 2

    return monitor.run()


if __name__ == "__main__":
    sys.exit(main())
