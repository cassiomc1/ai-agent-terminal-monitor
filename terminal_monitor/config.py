"""Monitor configuration dataclass and config-file (JSON/TOML) loading."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Optional TOML support (standard in Python 3.11+)
try:
    import tomllib
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef, unused-ignore]
    except ImportError:
        tomllib = None
from .safety import UNSAFE_PHRASES

# Per-user default state root: private by construction (item: state-directory
# permissions).  The historical default was the world-shared /tmp/terminal-monitor;
# that path is still honored when passed explicitly, but project scoping and
# 0o700 hardening now apply to this default root.
DEFAULT_STATE_DIR = str(Path.home() / ".cache" / "terminal-monitor")


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
    state_dir: str = DEFAULT_STATE_DIR
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
    objective: str = ""
    prohibitions: list[str] = field(default_factory=list)
    task_id: str = ""
    required_outcome: str = "merged"
    npm_publish_allowed: bool = False
    session_id: str = ""
    expected_branch: str = ""
    protected_branches: tuple[str, ...] = ("main", "master")
    report_path: str | None = None
    attempt_history_limit: int = 100
    loop_guard: bool = True
    loop_repeat_limit: int = 3
    queued_attempt_seconds: float = 45.0
    allow_history_rewrite: bool = False
    web_ui: bool = True
    web_port: int = 8765
    web_open_browser: bool = True
    loop_interrupt_wait_seconds: float = 2.0
    desktop_notifications: bool = True
    webhook_url: str = ""
    # Tunable policy windows (previously inline magic numbers).
    prompt_fast_threshold_seconds: float = 4.0
    protected_branch_nudge_window_seconds: float = 45.0
    launch_command: tuple[str, ...] = ()
    debug_log_path: str | None = None
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
                "objective": "Finish every task, create and merge the PR, then verify main.",
                "prohibitions": ["Do not publish to npm."],
                "task_id": "example-task",
                "required_outcome": "merged",
                "npm_publish_allowed": False,
                "session_id": "",
                "expected_branch": "",
                "protected_branches": ["main", "master"],
                "report_path": "",
                "attempt_history_limit": 100,
                "loop_guard": True,
                "loop_repeat_limit": 3,
                "queued_attempt_seconds": 45.0,
                "allow_history_rewrite": False,
                "web_ui": True,
                "web_port": 8765,
                "web_open_browser": True,
                "loop_interrupt_wait_seconds": 2.0,
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
objective = "Finish every task, create and merge the PR, then verify main."
prohibitions = ["Do not publish to npm."]
task_id = "example-task"
required_outcome = "merged"
npm_publish_allowed = false
session_id = ""
expected_branch = ""
protected_branches = ["main", "master"]
report_path = ""
attempt_history_limit = 100
loop_guard = true
loop_repeat_limit = 3
queued_attempt_seconds = 45.0
allow_history_rewrite = false
web_ui = true
web_port = 8765
web_open_browser = true
loop_interrupt_wait_seconds = 2.0

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
