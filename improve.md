# Suggested improvements for monitoring

These suggestions came from using the monitor alongside OpenCode on a real
project, with tasks that required resumption, permissions, CI, PRs, and merges.
All ten items below are implemented; the verification notes describe how.

## High priority

### 1. Separate "visual idleness" from "working process"

The terminal can stay on `Thinking`, `Preparing edit`, or `gh ... --watch`
for several minutes while child processes consume CPU. The monitor must
correlate the terminal snapshot with descendant processes, CPU, and command
age before sending a resumption. This avoids interrupting valid tests or
workflows.

Implemented: `ProcessActivity` observes descendant commands, CPU percent, and
command age; an active child counts as `thinking` and suppresses idle/question
automation.

### 2. Make resumption idempotent and observable

Every automatic send must carry an `attempt_id`, reason, timestamp, and
observed state. The state must distinguish `queued`, `sent`, `accepted`,
`completed`, and `ignored`, with a per-attempt cooldown. That way a message
never gets stuck in the queue without the monitor knowing whether it was
processed.

Implemented: `AttemptLedger` persists the full lifecycle with IDs and
timestamps; a visible `QUEUED` marker stays pending, duplicates are
suppressed, and a stale queue escalates to human attention.

### 3. Add an explicit "blocked on CI" protocol

Timeout cancellations, network failures, and `429`/timeout responses from
external sites are not equivalent to code failures. The monitor must classify
each check as `passed`, `failed`, `cancelled-infra`, or `failed-external`,
retry only the affected job, and record the evidence used for the decision.

Implemented: `classify_check_result` with external-failure markers; only
retryable runs are re-run and every classification is recorded as a CI event.

### 4. Enforce the merge gate inside the monitor itself

Before merging, re-query the full `headRefOid` SHA and require every check on
that SHA to be successfully completed. Cancellations must neither count as
success nor allow automatic merging. After the merge, verify the `main` SHA,
post-merge workflows, and a clean tree.

Implemented: `verify_merge_gate` + `merge_pull_request` with
`--match-head-commit`; `verify-final-state` covers the post-merge invariants.

## Medium priority

### 5. Detect changes outside the expected branch

If the agent returns to `main` with local changes, or switches branches
mid-run, the monitor must pause the flow and request a safe action: preserve,
commit on a branch, or discard only with explicit authorization. The status
must show branch, SHA, modified files, and the associated PR.

Implemented: repository-safety snapshots (`protected_branch_dirty`,
`branch_mismatch`, `not_a_repository`) pause supervision with the snapshot in
`attention.txt`; feature branches are auto-tracked (`BRANCH_TRACK`).

### 6. Use a risk-based permission policy

Safe, repeatable permissions may be approved automatically, but irreversible
actions (npm publication, release creation, deletions, and changes outside the
project) must be blocked by a rule independent of the text sent to the agent.
The npm prohibition must also appear in the persisted state and in the final
report.

Implemented: `PolicyEnvelope.authorize_action` / `compose` plus an independent
risk classifier; `npm_publish_allowed` defaults to `false` and is recorded in
`task-state.json` and `final-report.json`.

### 7. Support resumption after a monitor restart

Persist the agent session, last prompt, branch, PR, SHA, and workflow stage.
On restart, rebuild the state from GitHub and the terminal instead of
re-sending an instruction that may already be in flight.

Implemented: atomic `task-state.json` (identity, policy, stage, PR metadata,
attempts, interaction marker, generation); restart rebuilds from disk, GitHub,
and the live tab; corrupt state fails closed with `StateFileError`.

## Low priority

### 8. Improve the final report

Generate a structured summary with completed tasks, sent prompts, decided
permissions, merged PRs/commits, post-merge checks, any infrastructure
cancellations, and explicit confirmation that no npm publication happened.

Implemented: `final-report.json` with evidence, attempts, CI events, policy
decisions, prohibitions, and the npm-publication invariant.

### 9. Cover the supervisor with scenario tests

Add mocked tests for: stalled agent, long-running active command, permission
prompt, queued message, cancelled check, external `429`, changed SHA,
already-merged PR, and monitor restart. These scenarios matter more than just
testing terminal text extraction.

Implemented: scenario coverage in `tests/test_terminal_monitor.py` for all of
the above, including active-command suppression, stale-completion rejection,
and restart recovery.

### 10. Expose a simulation mode

A `--dry-run` mode must show which action the monitor would take without
sending keystrokes, approving permissions, or touching GitHub. This makes it
easy to validate policies before using the supervisor on a new project.

Implemented: `--dry-run` blocks terminal sends/keys, permission approvals,
agent restarts, merges, and CI workflow re-runs (`CI_RETRY_REQUIRED` stays put
and is logged instead of dispatching `gh run rerun`).

## Operational protections implemented

- The ledger distinguishes accepted delivery from a still-visibly `QUEUED`
  message, prevents duplicate resends, and escalates expired queues to human
  attention.
- The supervisor detects duplicate expensive suites/builds, repetitions
  without observable progress, and Git history-rewrite commands.
- Validated interruption shuts down the command's whole tree, from the
  deepest descendants to the requested process, without signaling the agent
  root; recovery waits for the exit, escalates to `SIGTERM`, and never injects
  a prompt while any descendant is still alive.
- Detections are exported in the structured status and can be tuned by
  configuration, keeping fail-closed defaults.
- The local web panel exposes only safe projections, shows a masked terminal
  snapshot, and keeps the log rotated to avoid leaks or unbounded growth.

## Implementation of this round

The five improvements prioritized from real usage plus the hardening observed
in the command-center review were implemented:

- signal-driven shutdown with final heartbeat, PID lock, and stale-state
  detection;
- JSON-safe, bounded, credential-masked `--once` inspection;
- colored `status` panel with task, progress, current command, Git, CI, and
  npm policy;
- dark web command center with redacted terminal snapshot, prompt/command-free
  status, and startup that releases the lock even when the chosen port cannot
  be used;
- operational documentation for the `/api/status`, `/api/events`, and
  `/api/terminal` endpoints, the redacted/rotated state files, and the
  `SIGINT` → wait → `SIGTERM` loop-recovery cycle;
- `stop`, `status`, and `resume` commands that never signal the agent
  process;
- regression tests and documentation in README/ABOUT for the new lifecycle.

## Implementation of the review round (v1.1.0)

- Fixed a real bug: `PullRequestStateMachine` kept `stage` and
  `seen_pr_number` as class attributes, sharing the PR lifecycle across all
  instances in the same process (SDK with multiple monitors and the test
  suite). State is now per instance, restorable from the persisted stage.
- The command-center `/api/instances` endpoint no longer depends on the fixed
  `/tmp/terminal-monitor` path: the server receives the monitor's state root
  (a custom `--state-dir` is now discovered by the instance picker).
- `POST /api/send` rejects bodies over 64 KiB with HTTP `413`, preventing a
  misbehaving tab from dumping unbounded data into the answer channel.
- New `--version` flag aligned with `pyproject.toml` (1.1.0).
- Regression tests covering: state-machine isolation between instances,
  persisted-stage restoration, `/api/instances` with a custom state root,
  413 rejection, and `--version` output.

## Implementation of the six-findings hardening round

A follow-up review produced six concrete findings; all are fixed and covered
by reproduction checks:

1. Mode-switch bypass: the post-`Tab` `continue_text` used to go straight to
   the backend with no policy check or ledger entry. It now flows through
   `policy.compose` + `authorize_action` + `_dispatch("mode_switch_continue")`,
   so an `npm publish` continuation is blocked with `ATTENTION_REQUIRED`
   instead of being typed into the agent.
2. Fail-closed final verification: empty results from failed queries used to
   read as a clean tree / no releases / no publication. Every invariant is
   now `True` only after its query succeeds (`evidence_complete` /
   `evidence_unknown` provenance), and completion is blocked while evidence is
   missing — no more synthetic `ok=True` reports.
3. Dry-run side effects: CI synchronization re-ran workflows even with
   `--dry-run`. The retry boundary now returns early (logged + recorded) and
   `retry_infrastructure_checks(..., dry_run=True)` is a no-op.
4. Truncated-history revalidation: losing the interaction marker made the
   whole current window look new, so an old completion message could finish a
   new task. `SessionTracker` now recovers small appends via line overlap on
   top of an incremental raw-length baseline and returns no segment (not the
   full window) when the marker is unrecognizably lost.
5. Dashboard controls vs CSP: inline `onclick`/`onchange`/`onsubmit`/`oninput`
   handlers conflicted with the nonce-based `script-src` policy, so Continue
   and DONE filtering silently did nothing. All controls now use
   `addEventListener` with `data-*` attributes.
6. Key-as-text: the panel's `KEY:tab` action was delivered as literal agent
   text. The answer channel is now explicitly typed — `KEY:<name>` routes to
   `send_key` (allowlisted, `manual_key` ledger reason) and unsupported names
   are rejected (HTTP `400` at `/api/send`, attention in the monitor).

## Implementation of the full-codebase hardening round

A comprehensive security, correctness, and concurrency audit resolved the
following gaps across the monitor, managed PTY host, web server, and CLI:

- **Managed PTY idle classification & fast-fail startup**: `ManagedPTYBackend.get_tab`
  no longer equates `status.alive` with `busy=True`, unblocking idle
  classification, smart nudges, and mode switching in managed sessions;
  `ManagedSessionClient._start_locked` now fails fast if the spawned host
  process exits early instead of polling until timeout.
- **SessionHost startup & stream-drain atomicity**: socket binding runs before
  forking the child process, session tokens use atomic `0o600` temp-file
  creation (`tempfile.mkstemp`), `_handle_connection` closes sockets on early
  errors, and `_finalize` coalesces full viewer queues so slow stream consumers
  still receive the terminal `exit` sentinel.
- **Supervision & policy integrity**: mode-switch `continue_text` is validated
  against `PolicyEnvelope` before sending the switch key, loop recovery/guard
  honor `--dry-run` before signaling PIDs, `branch_safety` nudges compose with
  `PolicyEnvelope` and respect `--dry-run`, `attention.txt` and `answer.txt`
  write atomically with `0o600`, `activity.commands` in `status.json` are
  redacted, `AgentProfile` singletons are cloned instead of mutated, and
  duplicate `get_pids()` / `collect_final_evidence()` work per step is removed.
- **Git, tmux, process tree, and CI helpers**: `_get_local_git_fields` keeps
  intact paths when `run_command` strips leading spaces on `git status --porcelain`,
  `_get_git_status_uncached` bypasses the open-PR cache too, notifications
  escape AppleScript safely and avoid quote mangling on `notify-send`, webhooks
  require HTTP(S) URLs, `TmuxBackend.send` uses `-l --` for literal payloads,
  `collect_process_activity` guards against cycles and excludes root PIDs, and
  `retry_infrastructure_checks` deduplicates workflow `run_id` retries.
- **Resume/stop verification, web API, & CLI parity**: `resume_monitor` validates
  argv structure/entrypoints strictly, `_monitor_process_matches` matches exact
  `--state-dir` and `terminal-monitor` / `supervisor.py` invocations, web routing
  strips query strings and rejects invalid/negative `Content-Length`, dashboard
  JS fixes `ansiToHtml`, `updatePipeline`, and `pollTimer` deduplication, and
  `cli.py` / `supervise.py` consistently handle `config.debug_log_path`,
  `continue_file`, `state_dir` backend wiring, and unknown `--remote-provider`.
