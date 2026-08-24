# Terminal Monitor Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the improvements recorded in `improve.md` while preserving the existing dependency-free monitor API and adding safe, observable supervision workflows.

**Architecture:** Extend the existing `TaskState`, `TerminalMonitor`, PR state machine, and CLI with small value-oriented helpers. Attempt records, CI classifications, policy decisions, repository safety, and final reports remain JSON-compatible and are persisted atomically in the existing state directory. The merge command re-queries the PR head and checks immediately before invoking `gh pr merge --match-head-commit`.

**Tech Stack:** Python 3.10+, standard library, `unittest`, Ruff, GitHub CLI when available.

---

### Task 1: Durable attempts, CI classifications, and risk policy

**Files:**
- Modify: `terminal_monitor.py`
- Test: `tests/test_terminal_monitor.py`

- [x] Add failing tests for attempt lifecycle records, explicit CI categories (`passed`, `failed`, `cancelled-infra`, `failed-external`), and blocked npm/release actions.
- [x] Run the focused tests and confirm the failures identify missing behavior.
- [x] Add backward-compatible `TaskState` fields for attempt records, CI events, policy decisions, last prompt, and expected branch.
- [x] Add `classify_check_result`, `classify_action_risk`, and a policy authorization helper; use them in the PR state machine, retry logic, and send path.
- [x] Record queued/sent/accepted/completed/ignored attempt events and expose them in status JSON.
- [x] Run focused tests and the complete existing suite.

### Task 2: Merge gate, branch safety, restart recovery, and reports

**Files:**
- Modify: `terminal_monitor.py`
- Test: `tests/test_terminal_monitor.py`

- [x] Add failing tests for exact-head merge gating, branch/worktree violations, restart-state persistence, and final report serialization.
- [x] Run the focused tests and confirm expected failures.
- [x] Implement `verify_merge_gate` and `merge_pull_request`, including `--match-head-commit`, with a dry-run result that never calls GitHub.
- [x] Implement repository safety snapshots and pause supervised nudges when an explicitly expected branch is violated or protected `main` is dirty.
- [x] Persist restart events and write `final-report.json` with evidence, attempts, CI classifications, policy decisions, and the npm prohibition result.
- [x] Run focused tests and the complete existing suite.

### Task 3: Complete dry-run and CLI/config integration

**Files:**
- Modify: `terminal_monitor.py`
- Modify: `supervisor.py`
- Modify: `.terminal-monitor.example.json`
- Modify: `.terminal-monitor.example.toml`
- Test: `tests/test_terminal_monitor.py`

- [x] Add failing tests proving dry-run never sends text, special keys, permissions, restart processes, or merge commands.
- [x] Add a `merge-pr` command and flags for expected SHA, dry-run, policy, and report output.
- [x] Make `supervise --dry-run` report the planned action and persisted policy without changing the terminal.
- [x] Add configuration fields for expected branch, report path, and attempt history limits to both starter formats.
- [x] Run parser, dry-run, and CLI smoke tests.

### Task 4: Documentation and scenario coverage

**Files:**
- Modify: `README.md`
- Modify: `ABOUT.md`
- Test: `tests/test_terminal_monitor.py`

- [x] Document attempt states, CI classifications, branch safety, exact-head merge gating, restart recovery, final reports, and dry-run semantics.
- [x] Add scenario tests for active command suppression, permission prompts, queued messages, cancelled checks, external `429`/timeout evidence, changed SHA, already-merged PRs, and restart recovery.
- [x] Run the full test suite, Ruff, `py_compile`, CLI help/config generation, and a requirements checklist against `improve.md`.

### Task 5: Integration

**Files:**
- Modify: only files required by tests or CI feedback.

- [ ] Commit the verified implementation and documentation on `codex/monitor-improvements`.
- [ ] Push the branch and create a GitHub PR with exact verification evidence and the npm non-publication invariant.
- [ ] Wait for checks on the fresh PR head; retry only cancelled/infrastructure or external-rate-limit checks and fix code failures with regression tests.
- [ ] Merge with the freshly queried full 40-character head SHA using the merge gate.
- [ ] Update local `main`, verify post-merge workflows, clean status, synchronized heads, and no npm publication.
