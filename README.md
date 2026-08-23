<div align="center">

# 🤖 AI Agent Terminal Monitor

**Universal, autonomous watcher and safe continuation driver for any AI coding agent CLI running in macOS Terminal.app, iTerm2, or tmux.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20WSL-brightgreen.svg)]()
[![Backends](https://img.shields.io/badge/backends-Terminal.app%20%7C%20iTerm2%20%7C%20tmux-orange.svg)]()
[![Zero Dependencies](https://img.shields.io/badge/dependencies-Zero%20External-success.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

</div>

---

## 📌 Overview

When running autonomous AI coding agents (such as **Anthropic Claude Code**, **OpenCode**, **Aider**, **Block Goose**, **Devin**, or custom in-house agent CLIs), agents frequently pause waiting for continuation prompts, permission grants, or selection choices.

**AI Agent Terminal Monitor** is a generic, modular, and extensible supervisor that watches your terminal, intelligently classifies agent lifecycle states (`thinking`, `permission`, `question`, `idle`), auto-resolves safe prompts, blocks unsafe/destructive operations, and nudges the agent to keep progressing autonomously.

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
│      [Thinking...] ──► [Permission Request] ──► [Question/Menu] ──► [Idle]  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                         Auto-Nudge / Attention / Log
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Target Agent CLI Tab                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## ✨ Key Features

- 🧠 **Multi-Agent Profile Engine**: Built-in specialized detection heuristics for **Claude Code**, **OpenCode**, **Aider**, and **Goose**, with zero-config support for any custom CLI (`generic`).
- 🖥️ **Universal Terminal Backends**:
  - `terminal`: Native macOS Terminal.app via AppleScript.
  - `iterm2`: Native macOS iTerm2 via AppleScript.
  - `tmux`: Native cross-platform tmux support (`capture-pane` & `send-keys`), enabling execution on **Linux, macOS, WSL, devcontainers, and remote SSH sessions**.
  - `auto`: Auto-detects active environment.
- 🛡️ **Fail-Closed Safety Engine**: Blocks dangerous actions (`rm -rf`, `delete`, `drop database`, `reset --hard`, `bypass`, etc.). Ambiguous or risky prompts halt the monitor with exit code `3` and export a snapshot to `attention.txt`.
- 📁 **Hierarchical Project Configuration**: Reads project settings from `.terminal-monitor.json` or `.terminal-monitor.toml` in your repository root, or globally from `~/.config/terminal-monitor/`.
- ✍️ **Live Human-in-the-Loop Override**: Need to guide the agent manually? Simply write your message into `/tmp/terminal-monitor/answer.txt` — it is consumed, dispatched, and cleaned up automatically.
- 🐍 **Python SDK & OOP API**: Clean object-oriented library API (`TerminalMonitor`, `MonitorConfig`, `AgentProfile`) with lifecycle hooks (`on_state_change`, `on_send`, `on_attention`, `on_tick`).
- ⚡ **Zero External Dependencies**: Pure Python standard library. No pip installation required.

---

## 🚀 Quick Start (CLI)

### 1. List Available Profiles
```bash
python3 terminal_monitor.py --list-profiles
```

### 2. Monitor Anthropic Claude Code
```bash
python3 terminal_monitor.py \
  --profile claude \
  --continue-text "Proceed with the remaining tasks and run the test suite."
```

### 3. Monitor OpenCode
```bash
python3 terminal_monitor.py \
  --profile opencode \
  --title my-project \
  --continue-text "Proceed from the next incomplete step."
```

### 4. Monitor Aider in a tmux session
```bash
python3 terminal_monitor.py \
  --profile aider \
  --backend tmux \
  --continue-file instructions.txt
```

### 5. Monitor Any Custom Agent CLI
```bash
python3 terminal_monitor.py \
  --process my-custom-agent \
  --continue-text "Continue" \
  --auto-allow-permissions
```

### 6. Inspect Without Sending (`--once` / `--dry-run`)
```bash
# One-shot inspection
python3 terminal_monitor.py --profile claude --once

# Monitor in dry-run mode (logs decisions without typing to terminal)
python3 terminal_monitor.py --profile claude --dry-run
```

To stop a running monitor, simply create the stop file:
```bash
touch /tmp/terminal-monitor/stop
```

---

## 📦 Project Configuration

Generate a configuration template in your repository root:

```bash
# JSON Format
python3 terminal_monitor.py --init-config json > .terminal-monitor.json

# TOML Format
python3 terminal_monitor.py --init-config toml > .terminal-monitor.toml
```

### Example `.terminal-monitor.json`
```json
{
  "profile": "claude",
  "process": "claude",
  "title": "my-project",
  "backend": "auto",
  "continue_text": "Proceed from the next incomplete step. Stop if you need human guidance.",
  "poll_seconds": 3.0,
  "idle_seconds": 15.0,
  "cooldown_seconds": 20.0,
  "gone_seconds": 25.0,
  "max_sends": 100,
  "auto_allow_permissions": false,
  "state_dir": "/tmp/terminal-monitor",
  "unsafe_phrases": [
    "bypass",
    "delete",
    "discard",
    "drop database",
    "force",
    "hard reset",
    "overwrite",
    "purge",
    "remove protection",
    "reset --hard",
    "rm -rf",
    "skip validation"
  ],
  "custom_profiles": {
    "my-agent": {
      "process": "myagent",
      "description": "Custom in-house agent profile",
      "thinking_patterns": ["thinking...", "running tool"],
      "permission_patterns": ["authorize action? [y/n]"],
      "auto_permission_payload": "y"
    }
  }
}
```

When `.terminal-monitor.json` or `.terminal-monitor.toml` is present in the working directory, simply run:
```bash
python3 terminal_monitor.py
```

---

## 🐍 Python API & SDK

Integrate Terminal Monitor directly into your Python scripts, orchestrators, or agent teams:

```python
from terminal_monitor import (
    TerminalMonitor,
    MonitorConfig,
    get_profile,
    get_backend,
)

# 1. Initialize configuration
config = MonitorConfig(
    profile="claude",
    continue_text="Proceed with the next pending task.",
    backend="auto",
    idle_seconds=10.0,
    max_sends=50,
)

# 2. Instantiate monitor
monitor = TerminalMonitor(config)

# 3. Attach event callbacks (Optional)
monitor.on_state_change = lambda old_st, new_st: print(f"🔄 State change: {old_st} -> {new_st}")
monitor.on_send = lambda reason, payload, ok: print(f"🚀 Sent ({reason}): {payload}")
monitor.on_attention = lambda reason, snapshot: print(f"⚠️ Attention required: {reason}")
monitor.on_tick = lambda state, pids: print(f"⏳ Waiting in state '{state}' (Active PIDs: {pids})")

# 4. Run loop or perform step-by-step control
monitor.run()
```

### Inspect Terminal State On-Demand:
```python
status = monitor.inspect()
print("Process Running:", status["ok"])
print("Detected State:", status["state"])
print("PID Count:", len(status["pids"]))
```

---

## 🛡️ Safety & Decision Engine

1. **Automated Safe Option Selection**:
   - When a numbered or bulleted prompt appears, recommended safe choices are automatically prioritized.
   - Safe preferred keywords (`continue`, `proceed`, `validate`, `allow`, `yes`) are matched.
   - If an option contains an unsafe phrase (`rm -rf`, `delete`, `drop database`, etc.) or is ambiguous, the monitor stops safely with **Exit Code 3** and dumps the terminal history to `attention.txt`.
2. **Interactive Answer Queue**:
   - Write your answer into `/tmp/terminal-monitor/answer.txt` at any time. The monitor consumes the answer, forwards it to the terminal tab, and deletes the file.
3. **Permission Grants**:
   - By default, permission prompts are **not** auto-approved. Pass `--auto-allow-permissions` only when running trusted workflows.

---

## ⚙️ CLI Reference

| Flag | Default | Description |
|---|---|---|
| `--profile` | `opencode` / config | AI agent profile (`claude`, `opencode`, `aider`, `goose`, `generic`) |
| `--process` | Profile process | Exact process name to monitor |
| `--title` | `None` | Filter by tab title or window name substring |
| `--backend` | `auto` | Terminal backend (`auto`, `terminal`, `iterm2`, `tmux`) |
| `--continue-text` | `""` | Text payload sent when the CLI becomes idle |
| `--continue-file` | `None` | UTF-8 file containing continuation instructions |
| `--config` | Auto-discovered | Path to custom `.json` or `.toml` config file |
| `--project-dir` | `.` | Directory to scan for `.terminal-monitor.*` configs |
| `--poll-seconds` | `3.0` | Polling interval in seconds |
| `--idle-seconds` | `15.0` | Seconds of inactivity before declaring idle state |
| `--cooldown-seconds` | `20.0` | Minimum cooldown between consecutive nudges |
| `--gone-seconds` | `25.0` | Seconds before exiting if process disappears |
| `--max-sends` | `100` | Maximum number of continuation nudges before exiting |
| `--auto-allow-permissions` | `false` | Automatically approve permission prompts |
| `--once` | `false` | Inspect tab once and exit with status code |
| `--dry-run` | `false` | Log monitor decisions without typing to terminal |
| `--state-dir` | `/tmp/terminal-monitor` | Directory for logs, `attention.txt`, and `answer.txt` |
| `--add-unsafe-phrase` | `[]` | Append additional unsafe phrase to blacklist |
| `--list-profiles` | `false` | Print all built-in and configured profiles |
| `--init-config` | `None` | Generate starter config file (`json` or `toml`) |

---

## 🧪 Testing

Run the full automated test suite (24 unit tests):

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile terminal_monitor.py
```

---

## 📄 License

MIT License. Open source and free for personal and commercial use.
