# About AI Agent Terminal Monitor

## 🎯 Motivation & Vision

Modern AI coding agents (such as **Claude Code**, **OpenCode**, **Aider**, **Goose**, or custom in-house LLM agents) represent a major leap forward in software engineering productivity. However, when executing long or complex multi-step workflows in a terminal, agents frequently encounter:

1. **Unattended Stalls**: The agent pauses when it finishes a subtask or turns idle, waiting for the human to type "continue" or "proceed".
2. **Permission Prompts**: Routine tool approvals or non-destructive confirmations pause execution until manually acknowledged.
3. **Choice Menus**: Simple multiple-choice questions with obvious or recommended defaults wait endlessly for user keyboard input.
4. **Destructive Risks**: Unconstrained automation can accidentally run hazardous commands (`rm -rf`, `reset --hard`, `drop table`, `delete`, etc.).

**AI Agent Terminal Monitor** was designed to solve these exact challenges: acting as an autonomous, fail-closed supervisor and companion driver that keeps your AI coding agents moving forward safely and efficiently.

---

## 🏛️ Architecture & Design Principles

### 1. Zero External Dependencies & Maximum Portability
- Built entirely on the **Python 3 Standard Library**.
- Runs immediately without requiring virtual environments, pip packages, or heavy external daemons.

### 2. Multi-Agent Specialization (`AgentProfile`)
Different AI agents use distinct visual conventions in the terminal:
- **Claude Code**: Shows dynamic status spinners, `thinking...`, `Allow this tool? [y/n]`, and arrow-key/numbered prompts.
- **Aider**: Uses pair-programming prompts, confirmation questions `(Y)es/(N)o`, and file-diff approval dialogues.
- **OpenCode**: Utilizes custom write confirmations, `esc interrupt` markers, and option bullets (`●`, `○`).
- **Generic / Custom**: Supports any CLI tool via configurable pattern dictionaries or regexes.

### 3. Pluggable Terminal Backends
- **macOS Terminal.app**: Native AppleScript tab scanning and non-intrusive script dispatch.
- **macOS iTerm2**: Native AppleScript session text extraction and command writing.
- **tmux**: Cross-platform pane capture (`tmux capture-pane`) and keystroke dispatch (`tmux send-keys`), enabling execution across **Linux, macOS, WSL, devcontainers, and remote headless servers**.

### 4. Fail-Closed Safety Model
- **Blacklist of Destructive Phrases**: Blocks dangerous actions by default.
- **Unambiguous Resolution**: Automatically chooses an option only when an explicit safe recommendation or preferred keyword exists.
- **Human-in-the-Loop Attention**: If a choice is ambiguous or potentially unsafe, the monitor halts with exit code `3` and exports the full terminal snapshot to `attention.txt`.
- **Live Answer Ingestion**: Allows developers to guide the running agent by dropping a message into `answer.txt` without touching the target terminal.

### 5. Multi-Project & Config Cascading
- Scans for `.terminal-monitor.json` or `.terminal-monitor.toml` in project directories.
- Falls back to user-global configuration in `~/.config/terminal-monitor/config.json`.
- Overridable via CLI arguments or Python SDK constructor options.

---

## 👥 Authors & Community

- **Maintainer**: [Cassio Marques Campos](https://github.com/cassiomc1)
- **Repository**: [https://github.com/cassiomc1/ai-agent-terminal-monitor](https://github.com/cassiomc1/ai-agent-terminal-monitor)
- **License**: [MIT License](LICENSE)
