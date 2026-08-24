# Supervisor v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver session-aware, policy-safe terminal supervision through verified PR merge without npm publication.

**Architecture:** Extend the existing dependency-free monitor with persistent task/session models, descendant-process activity, robust terminal identity, explicit operational commands, a PR/CI state machine, and a structured final verifier. Preserve all current APIs and configuration behavior.

**Tech Stack:** Python 3.10+, standard library, git and GitHub CLI integrations, unittest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-24-supervisor-v2-design.md`

## Global Constraints

- Preserve Python 3.10 support and the single-module package entry point.
- Preserve existing flags, backends, profiles, callbacks, and configuration files.
- `npmPublishAllowed` defaults to `false` and dynamic nudges cannot override it.
- Never interrupt the monitored root agent PID.
- Never accept completion evidence older than the latest sent instruction.
- Merge only with green checks for the exact PR head.

---

### Task 1: Session, activity, question, and policy behavior

**Files:**
- Modify: `terminal_monitor.py`
- Test: `tests/test_terminal_monitor.py`

**Interfaces:**
- Produces: `TaskState`, `ProcessActivity`, `SessionTracker`, `PolicyEnvelope`, and strong question gating.

- [ ] Write tests showing stale completion is ignored, an active descendant suppresses idle/question actions, `answer.txt` wins while thinking, and prohibitions survive smart nudges.
- [ ] Run the focused tests and confirm they fail for the missing behavior.
- [ ] Implement the smallest compatible models and monitor-flow changes.
- [ ] Run the focused tests and the existing suite.

### Task 2: Persistent state, terminal identity, and safe commands

**Files:**
- Modify: `terminal_monitor.py`
- Test: `tests/test_terminal_monitor.py`

**Interfaces:**
- Produces: atomic `<state-dir>/task-state.json`, default `<state-dir>/status.json`, `TerminalIdentity`, `send`, `interrupt-child`, and `restart-agent` commands.

- [ ] Write tests for state round trips, corrupt-state rejection, automatic status path, terminal-candidate scoring, root-PID protection, descendant signaling, and command parsing.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement storage, scoring, process-tree checks, and command handlers.
- [ ] Run focused tests and the suite.

### Task 3: PR/CI lifecycle and final verification

**Files:**
- Modify: `terminal_monitor.py`
- Test: `tests/test_terminal_monitor.py`

**Interfaces:**
- Produces: `PullRequestStage`, `classify_check_result`, `PullRequestStateMachine`, `FinalVerificationReport`, `verify_final_state`, and `verify-final-state` CLI.

- [ ] Write table-driven tests for all PR/CI stages, retryable infrastructure outcomes, exact-head checks, and each final invariant.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement the state machine and verifier with injected command execution for deterministic tests.
- [ ] Run focused tests and the suite.

### Task 4: Event-oriented waiting and documentation

**Files:**
- Modify: `terminal_monitor.py`
- Modify: `README.md`
- Modify: `.terminal-monitor.example.json`
- Modify: `.terminal-monitor.example.toml`
- Test: `tests/test_terminal_monitor.py`

**Interfaces:**
- Produces: bounded change-aware waiting and documented configuration/operations/migration guidance.

- [ ] Write a test proving the waiter returns early when watched state changes and remains bounded otherwise.
- [ ] Run it and confirm failure.
- [ ] Implement the waiter and wire it into the run loop.
- [ ] Update README and both starter configurations for all new concepts and commands.
- [ ] Run the full test suite, Ruff, CLI help smoke tests, and a requirements-to-tests checklist.

### Task 5: Integration

**Files:**
- Modify: only files required by review or CI feedback.

**Interfaces:**
- Produces: merged GitHub PR and verified synchronized `main`.

- [ ] Commit the verified changes and push `codex/supervisor-v2-hardening`.
- [ ] Create a PR against `main` with the verification evidence.
- [ ] Wait for checks; retry infrastructure-only failures and fix code failures with regression tests.
- [ ] Merge using the freshly queried exact head commit.
- [ ] Update local `main`, rerun the suite and Ruff, and verify local/remote tips and clean status.
