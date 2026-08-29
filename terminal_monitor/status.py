"""Human-readable status: ANSI dashboard, snapshot reading, and public projections."""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from .backends import run_command
from .gitinfo import get_git_status
from .processes import pid_is_alive
from .safety import redact_sensitive

ANSI_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "white": "\033[37m",
}


def _ansi(text: str, tone: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    return f"{ANSI_CODES.get(tone, '')}{text}{ANSI_CODES['reset']}"


def read_status_snapshot(state_dir: str | Path, project_dir: str = ".") -> dict[str, Any]:
    """Read live status plus durable state and mark stale monitor PIDs explicitly."""
    state_path = Path(state_dir)
    status_path = state_path / "status.json"
    task_path = state_path / "task-state.json"
    status: dict[str, Any] = {}
    task: dict[str, Any] = {}
    for path, target in ((status_path, status), (task_path, task)):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                target.update(loaded)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if not status:
        status = {
            "running": False,
            "state": "stopped",
            "timestamp": "",
            "git": {},
            "task": {},
        }
    monitor_pid = status.get("monitor_pid") or status.get("pid")
    pid_alive = bool(status.get("running") and pid_is_alive(monitor_pid))
    identity_checked = bool(status.get("monitor_instance_id") or status.get("schema_version", 1) >= 2)
    status["monitor_alive"] = bool(pid_alive and (not identity_checked or _monitor_process_matches(monitor_pid, state_path)))
    status["stale"] = bool(status.get("running") and monitor_pid and not status["monitor_alive"])
    if task:
        status["durable_task_state"] = task
        task_view = dict(status.get("task") or {})
        task_view.setdefault("task_id", task.get("taskId", ""))
        task_view.setdefault("stage", task.get("lastKnownStage", "TASK_RECEIVED"))
        task_view.setdefault("npm_publish_allowed", task.get("npmPublishAllowed", False))
        status["task"] = task_view
    if not status.get("git"):
        git = get_git_status(project_dir, ttl_seconds=0.0)
        status["git"] = {
            "branch": git.branch,
            "head": git.head,
            "dirty": git.dirty,
            "modified": git.modified_count,
            "untracked": git.untracked_count,
            "open_prs": git.open_prs_count,
            "last_commit": git.last_commit,
        }
    return status


def _status_monitor_label(snapshot: dict[str, Any]) -> tuple[str, str]:
    if snapshot.get("monitor_alive"):
        return "RUNNING", "green"
    if snapshot.get("stale"):
        return "STALE", "red"
    return "STOPPED", "yellow"


def render_status_dashboard(snapshot: dict[str, Any], *, color: bool = True, width: int = 78) -> str:
    """Render a compact ANSI dashboard suitable for a terminal or CI log."""
    monitor_label, monitor_tone = _status_monitor_label(snapshot)
    state = str(snapshot.get("state", "unknown")).upper()
    state_tone = "green" if state in {"THINKING", "COMPLETED"} else "yellow" if state in {"IDLE", "PERMISSION", "QUESTION"} else "red" if state in {"MISSING", "ATTENTION", "STOPPED"} else "cyan"
    task = snapshot.get("task") if isinstance(snapshot.get("task"), dict) else {}
    todo = snapshot.get("todo") if isinstance(snapshot.get("todo"), dict) else {}
    activity = snapshot.get("activity") if isinstance(snapshot.get("activity"), dict) else {}
    git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
    ci_events = snapshot.get("ci_events") if isinstance(snapshot.get("ci_events"), list) else []
    ci_categories = [str(item.get("category", "unknown")) for item in ci_events[-8:] if isinstance(item, dict)]
    ci_label = "green" if ci_categories and all(item == "passed" for item in ci_categories) else "attention" if any(item in {"failed", "failed-external"} for item in ci_categories) else "waiting" if ci_categories else "not observed"
    ci_tone = "green" if ci_label == "green" else "red" if ci_label == "attention" else "yellow" if ci_label == "waiting" else "dim"
    npm_allowed = bool(snapshot.get("npm_publish_allowed", task.get("npm_publish_allowed", False)))
    npm_label = "ALLOWED" if npm_allowed else "BLOCKED"
    npm_tone = "red" if npm_allowed else "green"
    branch = str(git.get("branch") or "-")
    head = str(git.get("head") or "")[:12] or "-"
    dirty = bool(git.get("dirty"))
    current_command = ""
    commands = activity.get("commands")
    if isinstance(commands, list) and commands:
        current_command = redact_sensitive(str(commands[0]))
    todo_total = int(todo.get("total", 0) or 0)
    todo_completed = int(todo.get("completed", 0) or 0)
    todo_pending = int(todo.get("pending", 0) or 0) + int(todo.get("in_progress", 0) or 0)
    progress = f"{todo_completed}/{todo_total}" if todo_total else "not detected"
    task_id = str(task.get("detected_id") or task.get("task_id") or "-")
    state_fragment = _ansi(state, state_tone, color)
    mode = str(snapshot.get("mode") or "-")
    lines = [
        _ansi("╭─ AI Agent Terminal Monitor", "cyan", color),
        _ansi(f"│ Monitor   {monitor_label:<7}  Agent ", monitor_tone, color) + f"{state_fragment:<10}" + _ansi(f"  Mode {mode!s:<6}", monitor_tone, color),
        _ansi(f"│ Heartbeat {snapshot.get('heartbeat') or snapshot.get('timestamp') or '-'}", "dim", color),
        _ansi("├─ Task", "blue", color),
        f"│ {task_id}  ·  progress {progress}  ·  remaining {todo_pending}",
        f"│ Stage     {task.get('stage') or snapshot.get('stage') or '-'}",
        _ansi("├─ Repository", "blue", color),
        f"│ {branch} @ {head}  ·  {'DIRTY' if dirty else 'clean'}  ·  open PRs {git.get('open_prs', 0)}",
        _ansi("├─ Safety & CI", "blue", color),
        f"│ npm { _ansi(npm_label, npm_tone, color) }  ·  CI { _ansi(ci_label, ci_tone, color) }",
    ]
    if current_command:
        wrapped = textwrap.wrap(current_command, width=max(20, width - 4)) or [current_command]
        lines.append(_ansi("├─ Current command", "blue", color))
        lines.extend(f"│ {line}" for line in wrapped[:3])
    else:
        lines.append(_ansi("├─ Current command", "blue", color))
        lines.append("│ no child command observed")
    if snapshot.get("stale"):
        lines.append(_ansi("│ Monitor PID is no longer alive; status file is stale.", "red", color))
    lines.append(_ansi("╰────────────────────────────────────────────────────────────────────────────", "cyan", color))
    return "\n".join(lines)
def _monitor_process_matches(pid: int | str | None, state_dir: str | Path) -> bool:
    """Verify a PID belongs to this monitor before sending it a signal."""
    if not pid_is_alive(pid):
        return False
    code, output, _ = run_command(["ps", "-p", str(pid), "-o", "command="])
    if code != 0:
        return False
    command = output.lower()
    if "terminal_monitor" not in command:
        return False
    state_path = Path(state_dir).resolve()
    state_candidates = {
        str(state_path).lower(),
        str(state_path.name).lower(),
        state_path.name.split("-")[0].lower(),
    }
    return any(candidate in command for candidate in state_candidates) or "terminal_monitor" in command


def stop_monitor(state_dir: str | Path, *, reason: str = "cli_stop") -> dict[str, Any]:
    """Stop only a verified monitor process and leave the agent process untouched."""
    state_path = Path(state_dir)
    status = read_status_snapshot(state_path)
    pid = status.get("monitor_pid") or status.get("pid")
    stop_path = state_path / "stop"
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.touch()
    result: dict[str, Any] = {"ok": True, "action": "stop", "state_dir": str(state_path), "pid": pid, "agent_untouched": True}
    if _monitor_process_matches(pid, state_path):
        try:
            os.kill(int(pid), signal.SIGTERM)
            result.update({"signal": "SIGTERM", "reason": reason})
        except OSError as exc:
            result.update({"ok": False, "error": str(exc)})
    else:
        result.update({"signal": "none", "reason": "no verified live monitor"})
    return result


def resume_monitor(state_dir: str | Path, *, project_dir: str = ".") -> dict[str, Any]:
    """Resume a previously launched supervisor from its validated launch metadata."""
    state_path = Path(state_dir)
    status = read_status_snapshot(state_path, project_dir)
    if status.get("monitor_alive"):
        return {"ok": True, "action": "resume", "already_running": True, "pid": status.get("monitor_pid")}
    metadata_path = state_path / "monitor.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "action": "resume", "error": f"launch metadata unavailable: {exc}"}
    command = [str(item) for item in metadata.get("command", [])] if isinstance(metadata, dict) else []
    state_candidates = {str(state_path).lower(), str(state_path.resolve()).lower()}
    command_text = " ".join(command).lower()
    trusted_entrypoint = "terminal_monitor.py" in command_text or "supervisor.py" in command_text
    if not command or not trusted_entrypoint or ("supervise" not in command_text and "--supervise" not in command_text) or not any(candidate in command_text for candidate in state_candidates):
        return {"ok": False, "action": "resume", "error": "refusing untrusted monitor launch metadata"}
    with contextlib.suppress(OSError):
        (state_path / "stop").unlink()
    try:
        process = subprocess.Popen(command, cwd=project_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return {"ok": False, "action": "resume", "error": str(exc)}
    return {"ok": True, "action": "resume", "pid": process.pid, "command": command, "agent_untouched": True}
def _public_attempt(record: dict[str, Any]) -> dict[str, Any]:
    """Return an attempt projection safe for local dashboard consumers."""
    public = {
        key: record.get(key)
        for key in ("attempt_id", "status", "timestamp", "reason", "observed_state")
        if record.get(key) is not None
    }
    if "payload" in record:
        public["payload_chars"] = len(str(record.get("payload") or ""))
    return public


def _redacted_commands(value: Any) -> list[str]:
    """Preserve command counts while keeping command text out of HTTP status."""
    if isinstance(value, (list, tuple)):
        return ["<redacted>" for _item in value]
    return []


def _public_activity(value: Any) -> dict[str, Any] | Any:
    """Project process activity without exposing arbitrary child command text."""
    if not isinstance(value, dict):
        return value
    activity = dict(value)
    activity["commands"] = _redacted_commands(activity.get("commands"))
    return activity


def _public_status(data: dict[str, Any]) -> dict[str, Any]:
    """Remove prompts and sensitive operational text before serving status over HTTP."""
    safe = dict(data)
    attempts = data.get("attempts", [])
    safe["attempts"] = [_public_attempt(item) for item in attempts if isinstance(item, dict)] if isinstance(attempts, list) else []
    safe.pop("last_prompt", None)
    prohibitions = data.get("prohibitions", [])
    safe["prohibitions"] = ["<configured>" for _item in prohibitions] if isinstance(prohibitions, (list, tuple)) else []
    safe["last_command"] = "<redacted>" if data.get("last_command") else ""
    safe["activity"] = _public_activity(safe.get("activity"))
    loop_guard = safe.get("loop_guard")
    if isinstance(loop_guard, dict):
        loop_guard = dict(loop_guard)
        loop_guard["activity"] = _public_activity(loop_guard.get("activity"))
        safe["loop_guard"] = loop_guard
    decisions = data.get("policy_decisions")
    if isinstance(decisions, list):
        safe["policy_decisions"] = [
            {
                key: item.get(key)
                for key in ("timestamp", "decision")
                if item.get(key) is not None
            }
            for item in decisions
            if isinstance(item, dict)
        ]
    return safe


def _public_event_line(line: str) -> str:
    """Redact free-form prompt payloads from the dashboard event stream."""
    redacted = redact_sensitive(line)
    return re.sub(r"\b(?:payload|prompt)=.*$", lambda match: match.group(0).split("=", 1)[0] + "=<redacted>", redacted, flags=re.IGNORECASE)
