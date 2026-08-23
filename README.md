<div align="center">

# 🤖 AI Agent Terminal Monitor

**Universal, autonomous watcher, TUI controller, and safe continuation supervisor for any AI coding agent CLI running in macOS Terminal.app, iTerm2, or tmux.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20WSL-brightgreen.svg)]()
[![Backends](https://img.shields.io/badge/backends-Terminal.app%20%7C%20iTerm2%20%7C%20tmux-orange.svg)]()
[![Zero Dependencies](https://img.shields.io/badge/dependencies-Zero%20External-success.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

</div>

---

## 📌 Overview

When running autonomous AI coding agents (such as **Anthropic Claude Code**, **OpenCode**, **Aider**, **Block Goose**, **Devin**, or custom in-house agent CLIs), agents frequently pause waiting for continuation prompts, permission grants, mode transitions, or selection choices.

**AI Agent Terminal Monitor** is a generic, modular, and extensible supervisor that watches your terminal, intelligently classifies agent lifecycle states (`thinking`, `permission`, `question`, `completed`, `idle`), auto-resolves safe prompts, manages TUI modes (`Plan` vs `Build`), blocks unsafe/destructive operations, generates context-aware Git nudges, exports real-time status JSON, and keeps the agent progressing autonomously to goal completion.

> **State precedence:** actionable states (`permission`, `question`, `completed`) are always detected before `thinking`. Agents frequently keep spinner hints like `esc to cancel` visible while a permission prompt is on screen, so the monitor never mistakes an actionable prompt for a busy state.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           AI Agent Terminal Monitor                         │
├──────────────────────────┬──────────────────────────┬───────────────────────┤
│    Supported Backends    │   Built-in AI Profiles   │     Safety Guard      │
│  • macOS Terminal.app    │  • Claude Code (claude)  │  • Destructive filter │
│  • macOS iTerm2          │  • OpenCode (opencode)   │  • Permission policy  │
│  • tmux (Linux/macOS)    │  • Aider (aider)         │  • Attention alerts   │
│  • Headless / Auto       │  • Goose & Generic CLI   │  • Live answer hook   │
└────────────┬─────────────┴────────────┬─────────────┴───────────┬───────────┘
             │                          │                         │
             ▼                          ▼                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                  Terminal History & State Classification                    │
│   [Thinking...] ──► [Permission] ──► [Question/Menu] ──► [Idle] ──► [Done]  │
│       │                                                            │        │
│   [TUI Mode] (Plan ──Tab──► Build)                [Git Context Smart Nudge] │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                         Auto-Nudge / Control / Status JSON
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Target Agent CLI Tab                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## ✨ Key Features

- 🧠 **Multi-Agent Profile Engine**: Built-in specialized detection heuristics for **Claude Code**, **OpenCode**, **Aider**, and **Goose**, with zero-config support for any custom CLI (`generic`).
- 🎛️ **TUI Mode Awareness & Auto-Transition**: Automatically detects agent interactive modes (e.g. `Plan` vs `Build` in OpenCode). When plan generation finishes, dispatches native switch keystrokes (`Tab`) and start approvals without human intervention.
- ⌨️ **Native Special Keys & Control Sequences**: Direct native dispatch for special characters (`Tab`, `Esc`, `Enter`, `Ctrl+C`, `Ctrl+P`) via native backend character codes without macOS Accessibility permission hurdles.
- 🌿 **Git-Aware Context Smart Nudges**: Inspects repository status dynamically to send targeted prompts:
  - *Uncommitted changes:* Prompts agent to run targeted tests and commit the task.
  - *Clean feature branch:* Prompts agent to run full verification, push branch and create PR.
  - *Open PRs:* Prompts agent to verify CI checks and merge into `main`.
- 🏁 **Completion Engine & Stop Conditions**: Detects when all plan tasks are 100% completed and merged, gracefully stopping the supervisor and firing completion events.
- 🔍 **Refined Question vs Table Disambiguation**: Excludes Markdown/Unicode summary tables and code blocks from option parsing, eliminating false-positive dialog loops.
- 📊 **Real-time Status JSON Export**: Continuously exports live structured JSON (`status.json`) with PIDs, state, mode, git details, uptime, and send counts for IDE or dashboard integrations.
- 🖥️ **Universal Terminal Backends**:
  - `terminal`: Native macOS Terminal.app via AppleScript.
  - `iterm2`: Native macOS iTerm2 via AppleScript.
  - `tmux`: Native cross-platform tmux support (`capture-pane` & `send-keys`), enabling execution on **Linux, macOS, WSL, devcontainers, and remote SSH sessions**.
  - `auto`: Auto-detects active environment.
- 🛡️ **Fail-Closed Safety Engine**: Blocks dangerous actions (`rm -rf`, `delete`, `drop database`, `reset --hard`, `bypass`, etc.). Ambiguous or risky prompts halt the monitor with exit code `3` and export a snapshot to `attention.txt`.
- ⏱️ **Hard Timeouts & Resilience**: All subprocess calls (`osascript`, `git`, `gh`, `tmux`) run with hard timeouts, so a stuck terminal dialog or hung network call can never freeze the monitor. `Ctrl+C` exits cleanly (exit code `130`) and marks the status JSON as not running.
- 🔐 **Input Validation**: Process names and window-title filters are validated/sanitized before being embedded into AppleScript or shell commands.
- 📁 **Hierarchical Project Configuration**: Reads project settings from `.terminal-monitor.json` or `.terminal-monitor.toml` in your repository root, or globally from `~/.config/terminal-monitor/`. Unsafe phrases from CLI flags (`--unsafe-phrase`) are merged with the ones from the config file instead of replacing them.
- 🗂️ **Cached Git Context**: Repository status is cached with a 30-second TTL, keeping the polling loop cheap even with live status JSON export enabled.
- ✍️ **Live Human-in-the-Loop Override**: Write a message into `/tmp/terminal-monitor/answer.txt` — it is consumed, dispatched, and cleaned up automatically.
- 🐍 **Python SDK & OOP API**: Clean object-oriented library API (`TerminalMonitor`, `MonitorConfig`, `AgentProfile`) with lifecycle hooks (`on_state_change`, `on_mode_change`, `on_send`, `on_attention`, `on_complete`, `on_tick`).
- ⚡ **Zero External Dependencies**: Pure Python standard library. No pip installation required.

---

## 🚀 Quick Start (CLI)

### 1. Autonomous Supervision Mode (`supervise` / `-S`)
Run full autonomous supervision with automatic mode switching, safe permission approval, Git-aware smart nudges, real-time status export, and completion detection:

```bash
python3 terminal_monitor.py supervise \
  --profile opencode \
  --status-json /tmp/terminal-monitor/status.json
```

### 2. Monitor Anthropic Claude Code
```bash
python3 terminal_monitor.py \
  --profile claude \
  --continue-text "Proceed with the remaining tasks and run the test suite."
```

### 3. Monitor OpenCode in a specific window/tab
```bash
python3 terminal_monitor.py \
  --profile opencode \
  --title my-project \
  --auto-allow-permissions
```

### 4. Monitor Aider in a tmux session
```bash
python3 terminal_monitor.py \
  --profile aider \
  --backend tmux \
  --continue-file instructions.txt
```

### 5. Inspect Without Sending (`--once` / `--dry-run`)
```bash
# One-shot inspection
python3 terminal_monitor.py --profile opencode --once

# Monitor in dry-run mode (logs decisions without typing to terminal)
python3 terminal_monitor.py --profile claude --dry-run
```

### 6. Graceful Stop
To gracefully stop a running supervisor or monitor daemon:
```bash
touch /tmp/terminal-monitor/stop
```

---

## 📦 Project Configuration

Generate a configuration template in your repository root:

```bash
# JSON Format
python3 terminal_monitor.py init --format json -o .terminal-monitor.json

# TOML Format
python3 terminal_monitor.py init --format toml -o .terminal-monitor.toml
```

### Example `.terminal-monitor.json`
```json
{
  "profile": "opencode",
  "process": "opencode",
  "title": "my-project",
  "backend": "auto",
  "continue_text": "Continue with the next task according to the plan.",
  "poll_seconds": 3.0,
  "idle_seconds": 15.0,
  "cooldown_seconds": 20.0,
  "gone_seconds": 25.0,
  "max_sends": 100,
  "auto_allow_permissions": true,
  "supervise": true,
  "smart_nudges": true,
  "auto_switch_modes": true,
  "completion_check": true,
  "state_dir": "/tmp/terminal-monitor",
  "status_json_path": "/tmp/terminal-monitor/status.json",
  "unsafe_phrases": [
    "bypass",
    "delete",
    "discard",
    "drop database",
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
    "weaken"
  ],
  "custom_profiles": {
    "my-agent": {
      "process": "myagent",
      "description": "Custom agent CLI configuration",
      "thinking_patterns": ["agent is thinking...", "processing..."],
      "permission_patterns": ["do you authorize this action?"],
      "auto_permission_payload": "y",
      "mode_patterns": { "plan": "Plan Mode", "build": "Build Mode" },
      "completion_patterns": ["all tasks complete"]
    }
  }
}
```

---

## 🐍 Python SDK API

You can embed `TerminalMonitor` directly into your Python tools, test suites, or custom agent frameworks:

```python
from terminal_monitor import (
    TerminalMonitor,
    MonitorConfig,
    get_profile,
    get_backend,
)

# 1. Configure monitor
config = MonitorConfig(
    process="opencode",
    profile="opencode",
    supervise=True,
    smart_nudges=True,
    auto_switch_modes=True,
    completion_check=True,
    status_json_path="/tmp/terminal-monitor/status.json",
)

# 2. Instantiate monitor
monitor = TerminalMonitor(config)

# 3. Register lifecycle hooks
monitor.on_state_change = lambda old_state, new_state: print(f"State: {old_state} -> {new_state}")
monitor.on_mode_change = lambda old_mode, new_mode: print(f"TUI Mode: {old_mode} -> {new_mode}")
monitor.on_send = lambda reason, payload, ok: print(f"Sent [{reason}]: {payload}")
monitor.on_complete = lambda snapshot: print("Task execution finished successfully!")
monitor.on_attention = lambda reason, snapshot: print(f"⚠️ Human attention needed ({reason})")

# 4. Run loop (or call monitor.step() for single iteration)
exit_code = monitor.run()
```

---

## 📊 Live Status JSON Structure

When `--status-json` or `config.status_json_path` is specified, `TerminalMonitor` continuously maintains a live status export:

```json
{
  "running": true,
  "pids": [21353],
  "process": "opencode",
  "profile": "opencode",
  "state": "thinking",
  "mode": "build",
  "sends": 14,
  "stable_seconds": 12.4,
  "git": {
    "branch": "feat/rc6-closing-fixes",
    "dirty": false,
    "modified": 0,
    "untracked": 0,
    "open_prs": 1,
    "last_commit": "d78ae3d fix(types): commit ambient declarations"
  },
  "timestamp": "2026-08-23T19:45:00Z"
}
```

---

## 🧪 Running Tests & Lint

Run the complete built-in unit test suite (zero external test dependencies):

```bash
python3 -m unittest discover -s tests -v
```

Lint checks (used by CI) run via [ruff](https://docs.astral.sh/ruff/):

```bash
pip install ruff
ruff check terminal_monitor.py supervisor.py tests/
```

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
