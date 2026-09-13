# Supervisor v2 Design

## Goal

Make long-running agent supervision session-aware, policy-safe, observable, and able to carry a repository task through PR creation, CI, merge, and post-merge verification without confusing stale terminal output with current work.

## Architecture

The monitor remains a dependency-free Python CLI. New behavior is divided into focused value objects and services inside the `terminal_monitor` package (see `README.md` for the module layout) to preserve the existing package contract:

- `TaskState` persists the task identity, policy, stage, PR metadata, and terminal identity in `<state-dir>/task-state.json`.
- `ProcessActivity` observes descendants of the agent process and distinguishes a quiet terminal from a running child command.
- `SessionTracker` records an interaction generation plus an incremental history-length baseline and only accepts completion evidence produced after the latest sent instruction; small appends past the normalization window are recovered by line overlap, and an unrecognizably lost marker yields no segment (fail-closed).
- `PolicyEnvelope` composes the permanent objective and prohibitions with stage-specific nudges. Dynamic text is rejected if it conflicts with permanent prohibitions.
- `PullRequestStateMachine` maps GitHub PR/check data into explicit stages and distinguishes code failures from cancelled, timed-out, network, and infrastructure results.
- `FinalVerifier` produces a structured report for the exact PR head, local/remote branch alignment, worktree cleanliness, registry/tag/release stability, and publish-process absence. Every invariant is `True` only after its query succeeds (`evidence_complete` / `evidence_unknown` provenance); failed queries are unknown, never clean, and block success.
- Operator answer payloads are explicitly typed: `KEY:<name>` routes to `send_key` as a `manual_key` ledger entry (allowlisted names only). Mode-switch continuations (`mode_switch` → `mode_switch_continue`) and all other sends share the single policy-checked dispatch path, and `--dry-run` additionally blocks CI workflow re-runs.

Backends receive a `TerminalIdentity` containing project path, branch, session id, title, and root PID. Existing `process` and `title` selection remains supported for compatibility.

## State and data flow

Every supervision iteration follows this order:

1. Read and consume `answer.txt` before any `thinking` or cooldown guard.
2. Resolve the terminal using all configured identity hints.
3. Capture terminal history and descendant-process activity.
4. Update the session generation and persistent task state.
5. Classify permission/question/completion only from the current interaction segment. A question requires a strong prompt plus selectable options and no active child command.
6. Advance the PR/CI stage when repository metadata is available.
7. Act according to permanent policy, current stage, and safe automatic choices.
8. Write both `status.json` and `task-state.json` atomically.
9. Wait using file/process/repository change signals, with bounded polling only as a portability fallback.

## Operational commands

- `send`: send an explicit message through the selected terminal.
- `interrupt-child`: signal only a verified descendant command, never the monitored root agent.
- `restart-agent --continue-session`: restart the configured command with its saved session identifier.
- `verify-final-state`: run the built-in repository/PR/release safety checks and return non-zero when any required invariant fails.

## Safety invariants

- The root agent PID is never interrupted by `interrupt-child`.
- `npmPublishAllowed` defaults to `false`; no nudge can override this prohibition.
- Completion text from an older interaction cannot finish a newer task.
- Active child work suppresses question/idle automation.
- A cancelled, timed-out, network, or infrastructure check is retryable and is not reported as a code failure.
- Merge is permitted only when checks are green for the exact saved PR head.
- Final success requires merged PR, exact head validation, synchronized clean `main`, unchanged npm registry state, no new tag/release, and no active publish process — each positively verified, with missing evidence blocking the verdict.
- Dashboard controls use `addEventListener` with `data-*` attributes (no inline event handlers) so they remain functional under the nonce-based Content Security Policy; key actions are allowlisted at the HTTP boundary.

## Compatibility and errors

Existing flags, configuration files, backends, profiles, status callbacks, and direct `TerminalMonitor` use remain valid. New state fields use safe defaults and tolerate older state files. Corrupt state fails closed with an explanatory error. External command failures return structured evidence rather than being silently interpreted as success.

## Testing

Unit tests cover stale completion rejection, activity detection, manual-answer priority, strong question gating, policy composition, state persistence, PR/CI transitions, terminal scoring, child-only interruption, default status export, CLI parsing, and final verification. Existing tests remain regression coverage. The full suite and Ruff run before PR creation and again after merge.
