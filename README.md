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

> **State precedence:** explicit manual answers and safe permission prompts take priority. A question requires both a strong prompt and real selectable options, while an active child command counts as `thinking`. Completion is accepted only from output produced after the latest instruction, so an old “done” message cannot finish a newly assigned task.

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
- 🧭 **Session Generations**: Separates terminal scrollback from the current interaction and rejects stale completion evidence after new work is assigned.
- ⚙️ **Real Process Activity**: Observes descendant commands, command age and CPU data, preventing a quiet terminal from being treated as stalled while tests or builds are still running.
- 📜 **Durable Policy Envelope**: Stores the objective, prohibitions, task ID, required outcome, current stage, PR metadata, and session ID in `task-state.json`. Smart nudges are wrapped in permanent policy and cannot override an npm-publication prohibition.
- 🔁 **Native PR/CI Lifecycle**: Tracks `PR_CREATED → CI_PENDING → FIX_REQUIRED` or `CI_RETRY_REQUIRED → CI_GREEN → POST_MERGE_VERIFY`; infrastructure-like cancellations/timeouts are retried without being mislabeled as code failures.
- ✅ **Final-State Verifier**: Verifies merged PR, checks for the exact PR head, synchronized clean `main`, unchanged npm registry state, no new tag/release, and no active publish process.
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
  --objective "Finish every task, create and merge the PR, then verify main." \
  --prohibition "Do not publish to npm."
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

### 7. Operational control commands

```bash
# Explicit operator message; this also outranks a visible "thinking" spinner
python3 terminal_monitor.py send "Resume the remaining work" --profile opencode

# Interrupt only PID 43210 when it is verified as a descendant of the agent
python3 terminal_monitor.py interrupt-child --pid 43210 --profile opencode

# Restart and continue the session saved in task-state.json
python3 terminal_monitor.py restart-agent --continue-session --profile opencode

# Verify repository, PR, CI, release and npm invariants
python3 terminal_monitor.py verify-final-state --project-dir . --state-dir /tmp/terminal-monitor
```

`interrupt-child` refuses the root agent PID and any PID outside its descendant tree. `restart-agent` executes an argument vector directly without a shell. For a non-OpenCode CLI, use `--agent-command` when its continuation syntax differs.

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
  "objective": "Finish every task, create and merge the PR, then verify main.",
  "prohibitions": ["Do not publish to npm."],
  "task_id": "work-42",
  "required_outcome": "merged",
  "npm_publish_allowed": false,
  "session_id": "ses_123",
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
    objective="Finish every task, merge the PR, and verify main.",
    prohibitions=["Do not publish to npm."],
    task_id="work-42",
    session_id="ses_123",
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

Whenever a state directory exists, `TerminalMonitor` atomically maintains `<state-dir>/status.json`; `--status-json` can redirect it. It also maintains `<state-dir>/task-state.json` with durable task and PR state.

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
  "activity": {
    "active": true,
    "descendants": [21401],
    "commands": ["python3 -m unittest discover -s tests"],
    "cpu_percent": 74.2,
    "oldest_seconds": 18.0
  },
  "task": {
    "task_id": "work-42",
    "stage": "CI_PENDING",
    "session_generation": 3,
    "npm_publish_allowed": false
  },
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

## PR/CI and final-verification stages

The supervisor persists these stages instead of inferring progress only from terminal prose:

| Stage | Meaning | Automatic action |
|---|---|---|
| `TASK_RECEIVED` | Work is active | Continue from the task plan |
| `PR_CREATED` / `CI_PENDING` | PR exists and checks are incomplete | Wait for a state change |
| `FIX_REQUIRED` | A code check failed | Return the agent to diagnosis and tests |
| `CI_RETRY_REQUIRED` | Cancel, timeout, network or infrastructure failure | Retry only the affected workflow run |
| `CI_GREEN` | Exact PR head is green | Proceed to merge |
| `POST_MERGE_VERIFY` | PR is merged | Run `verify-final-state` invariants |

`answer.txt`, `stop`, process-tree changes, git changes, and CI stage changes can wake supervision early; a bounded timer remains as a portable fallback.

## Migration notes

- Existing configurations remain valid.
- `status.json` is now created by default inside `state_dir`; remove consumers that assumed it existed only with `--status-json`.
- `npm_publish_allowed` defaults to `false`. Enabling publication requires the explicit `--allow-npm-publish` flag or configuration value.
- A malformed `task-state.json` stops initialization with `StateFileError` instead of silently discarding safety policy.

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
