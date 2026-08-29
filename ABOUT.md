# About AI Agent Terminal Monitor

## 🎯 Motivation & Vision

Modern AI coding agents (such as **Claude Code**, **OpenCode**, **Aider**, **Goose**, or custom in-house LLM agents) represent a major leap forward in software engineering productivity. However, when executing long or complex multi-step workflows in a terminal, agents frequently encounter:

1. **Unattended Stalls**: The agent pauses when it finishes a subtask or turns idle, waiting for the human to type "continue" or "proceed".
2. **Permission Prompts**: Routine tool approvals or non-destructive confirmations pause execution until manually acknowledged.
3. **Choice Menus**: Simple multiple-choice questions with obvious or recommended defaults wait endlessly for user keyboard input.
4. **TUI Mode Barriers**: Agents often operate across distinct TUI modes (e.g. `Plan` vs `Build` modes), requiring specific control keys (such as `Tab`) to approve specs and transition into code generation.
5. **Destructive Risks**: Unconstrained automation can accidentally run hazardous commands (`rm -rf`, `reset --hard`, `drop table`, `delete`, etc.).
6. **Task Completion Blindness**: Without completion detection, monitors can continue nudging agents after all tasks and pull requests are already completed and merged.

**AI Agent Terminal Monitor** was designed to solve these exact challenges: acting as an autonomous, fail-closed supervisor and companion driver that keeps your AI coding agents moving forward safely and efficiently.

---

## 🏛️ Architecture & Design Principles

The architecture is shown as a live, dark-theme SVG with animated signal paths,
policy stops and evidence lanes: [open the animated architecture diagram](docs/architecture-diagram.svg).
Its visual language follows the referenced [Archify Proof Lab gallery](https://tt-a1i.github.io/archify/gallery.html#proof-web-app): a dark live artifact, trace lanes, and explicit evidence surfaces.

### 1. Zero External Dependencies & Maximum Portability
- Built entirely on the **Python 3 Standard Library**.
- Runs immediately without requiring virtual environments, pip packages, or heavy external daemons.

### 2. Multi-Agent Specialization (`AgentProfile`)
Different AI agents use distinct visual conventions in the terminal:
- **Claude Code**: Shows dynamic status spinners, `thinking...`, `Allow this tool? [y/n]`, and arrow-key/numbered prompts.
- **OpenCode**: Utilizes custom write confirmations, `esc interrupt` markers, option bullets (`●`, `○`), and distinct `Plan` / `Build` TUI modes.
- **Aider**: Uses pair-programming prompts, confirmation questions `(Y)es/(N)o`, and file-diff approval dialogues.
- **Generic / Custom**: Supports any CLI tool via configurable pattern dictionaries or regexes.

### 3. Native Control Keys & Special Sequences
- Dispatches special keystrokes (`Tab`, `Esc`, `Enter`, `Ctrl+C`, `Ctrl+P`, arrow keys) directly via native AppleScript character codes in macOS Terminal.app and iTerm2, and via key symbols in tmux.
- Eliminates macOS Accessibility permission requirements and prevents AppleScript error `1002` (System Events sandbox restrictions).

### 4. TUI Mode Awareness & Lifecycle Management
- Identifies active agent modes (e.g. `Plan` vs `Build`).
- Detects when planning is complete and automatically sends mode-switch keystrokes and start authorizations to trigger code implementation.

### 5. Git Context-Aware Smart Nudges
- Analyzes repository status dynamically (modified files, untracked changes, unpushed branches, open GitHub pull requests via `gh`).
- Emits intelligent, contextually relevant continuation prompts tailored to the agent's current progress.

### 6. Completion Engine & Stop Conditions
- Detects final plan completion indicators (`"100% concluído"`, `"all tasks completed"`, clean working tree).
- Gracefully stops the supervisor daemon and triggers completion events (`on_complete`).

### 7. Pluggable Terminal Backends
- **macOS Terminal.app**: Native AppleScript tab scanning and non-intrusive script dispatch.
- **macOS iTerm2**: Native AppleScript session text extraction and command writing.
- **tmux**: Cross-platform pane capture (`tmux capture-pane`) and keystroke dispatch (`tmux send-keys`), enabling execution across **Linux, macOS, WSL, devcontainers, and remote headless servers**.

### 8. Fail-Closed Safety Model
- **Blacklist of Destructive Phrases**: Blocks dangerous actions by default.
- **Unambiguous Resolution**: Automatically chooses an option only when an explicit safe recommendation or preferred keyword exists.
- **Human-in-the-Loop Attention**: If a choice is ambiguous or potentially unsafe, the monitor halts with exit code `3` and exports the full terminal snapshot to `attention.txt`.
- **Live Answer Ingestion**: Allows developers to guide the running agent by dropping a message into `answer.txt` without touching the target terminal.

### 9. Multi-Project & Config Cascading
- Scans for `.terminal-monitor.json` or `.terminal-monitor.toml` in project directories.
- Falls back to user-global configuration in `~/.config/terminal-monitor/config.json`.
- Overridable via CLI arguments or Python SDK constructor options.
- Custom unsafe phrases passed via CLI flags are merged with (never replace) the ones from config files, and de-duplicated.

### 10. Resilience & Resource Safety
- **Hard subprocess timeouts**: every `osascript`, `git`, `gh`, and `tmux` invocation runs with a hard timeout, so a modal dialog in the terminal or a hung network call can never freeze the monitor loop.
- **Cached Git context**: repository snapshots (`git status`, `gh pr list`) are cached with a 30-second TTL per project directory, keeping high-frequency polling and live status JSON export cheap even on large repositories or slow networks.
- **Input validation**: process names must match `[A-Za-z0-9_.-]+` and window-title filters reject control characters before being embedded into AppleScript literals or shell commands (defense-in-depth against injection).
- **Stable Terminal.app targeting**: a custom tab title is preferred over the application suffix in the window name, preventing a generic `OpenCode` filter from selecting a neighboring agent session.
- **Clean interruption**: `Ctrl+C` during the monitor loop logs the exit, writes `"running": false` to the status JSON, and returns exit code `130`.
- **Signal-safe lifecycle**: `SIGTERM` is handled like a graceful stop, with
  a final heartbeat, lifecycle reason, and a PID lock that prevents duplicate
  supervisors. The `stop`, `status`, and `resume` commands never signal
  the monitored agent.
- **Attempt and restart journal**: every automated continuation is persisted with an ID and lifecycle status, while restart events retain the saved session and last prompt context.
- **Queue-aware delivery**: visible `QUEUED` output remains pending instead of being mistaken for acceptance; duplicate sends stop and a stale queue becomes an attention event.
- **Loop containment**: duplicate expensive process roots, repeated test/build episodes without Git or task progress, and Git history rewrites pause supervision. Verified child interruption signals the complete descendant tree deepest-first so wrappers do not leave orphan processes behind.
- **Session-preserving recovery**: repeated test/build loops are recovered by interrupting verified expensive children only, waiting for the entire child tree to stop, escalating to `SIGTERM` when necessary, and sending a diagnostic prompt into the existing session only after the stop is confirmed; the monitored root PID is never a recovery target.
- **Local command center**: continuous monitoring automatically serves and opens a localhost-only dark dashboard with color-coded live events, a bounded redacted terminal snapshot, safe status projections, bounded rotating logs, process/task/Git summaries, and a compact operations-console visual language.
- **External-check distinction**: rate limits (`429`), timeouts, and network failures are recorded as retryable external evidence rather than being confused with code regressions.

### 11. Merge and Repository Safety
- **Exact-head merge gate**: the `merge-pr` command re-reads the PR head and check rollup immediately before merging and supplies `--match-head-commit` to GitHub CLI.
- **Protected branch guard**: supervised work pauses on dirty `main`/`master` or a configured unexpected branch, preserving the snapshot in `attention.txt`.
- **Auditable reports**: `final-report.json` records verification evidence, attempts, CI events, policy decisions, the explicit npm prohibition, and the npm-publication invariant.
- **Dry-run isolation**: dry-run mode prints decisions without sending terminal input, approving permissions, starting agents, or merging PRs.

### 12. State Classification Precedence
- Actionable states (`permission`, `question`, `completed`) are classified before `thinking`.
- Rationale: agents keep spinner hints like `esc to cancel` visible while permission prompts or menus are on screen; checking "busy" markers first would deadlock the monitor on actionable prompts.

### 13. Operator-facing status
- **Colored dashboard**: `status` presents monitor liveness, agent state,
  task progress, current command, repository/CI stage, and the npm policy in a
  compact ANSI dashboard; `--json` remains available for automation.
- **Useful task progress**: the monitor extracts and deduplicates common TUI
  todo markers, reports completed/in-progress/pending counts, and exposes a
  best-effort current task identifier without changing durable task identity.
  When a terminal overlays a stale Todo pane over the conversation, an
  affirmative summary such as `35/35 COMPLETE` is treated as authoritative;
  question text is never treated as completion evidence.
- **Safe inspection**: `--once` returns JSON-safe dataclasses, bounds terminal
  history, and masks common tokens, API keys, passwords, Bearer credentials, and
  GitHub tokens before writing inspection or attention artifacts.

### 14. Archify-Inspired Web Command Center & Architecture Pipeline

The continuous monitor owns a localhost-only web server with a visual system inspired by the
[Archify Proof Lab](https://tt-a1i.github.io/archify/gallery.html#proof-web-app). It provides:
- **Architecture Stage Pipeline:** Interactive step sequence tracking lifecycle progress across `TASK_RECEIVED → EXECUTING → VERIFYING → PR_CREATED → CI_CHECKS → MERGED`.
- **Task Plan & Progress:** Categorized task breakdown with live badges (`DONE`, `ACTIVE`, `TODO`), search filter, and category pills (`ALL`, `ACTIVE`, `PENDING`, `DONE`).
- **Real-Time Streaming:** Server-Sent Events (`/api/stream`) for low-latency, event-driven updates.
- **Operator Action Dispatch:** Quick buttons (`Approve (yes)`, `Continue`, `Mode (Tab)`, `Nudge`) and custom instruction prompt input dispatched directly to the monitor via `POST /api/send`.
- **Privacy & Safety Projections:** The JSON projection intentionally masks credentials, prompts, attempt payloads, configured prohibitions, and policy actions while serving status safely over HTTP.

### 15. Supervisor Intelligence & State Isolation
- **Project-Level State Isolation:** Automatically scopes monitor state, logs, and artifacts per project in `~/.cache/terminal-monitor/<project-name>-<hash>/` (per-user and private by construction; an explicit `--state-dir`, including the historical `/tmp/terminal-monitor`, is still honored).
- **Automatic CWD Discovery:** Automatically resolves the target agent process directory via `lsof` or `/proc` when `--project-dir` is not explicitly passed.
- **Smart Protected Branch Nudges:** Rather than crashing or halting when `main` has uncommitted changes, automatically sends a guidance nudge instructing the agent to create a feature branch before enforcing repository safety gates.
- **Task Title Reconciliation:** Expands truncated TUI checklist labels into full task descriptions discovered in planning blocks throughout the terminal history.

---

## 👥 Authors & Community

- **Maintainer**: [Cassio Marques Campos](https://github.com/cassiomc1)
- **Repository**: [https://github.com/cassiomc1/ai-agent-terminal-monitor](https://github.com/cassiomc1/ai-agent-terminal-monitor)
- **License**: [MIT License](LICENSE)
