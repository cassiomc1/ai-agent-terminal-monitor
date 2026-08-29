# Code Improvement Suggestions — AI Agent Terminal Monitor

Analysis date: 2026-08-29 · Codebase: `terminal_monitor.py` (~4,977 lines), `supervisor.py`, `tests/` (126 tests passing, 1 skipped)

This document lists concrete, prioritized improvements found during a full code review. Each item explains **what** to improve, **why** it matters, and **how** to implement it.

---

## 🔴 High Priority — Security

### 1. Add authentication to the web command center POST endpoints

**What:** `POST /api/send` and `/api/answer` accept any local request with no authentication. Any local process — or a malicious webpage using DNS rebinding — can inject arbitrary prompts into the supervised agent session, effectively controlling the AI agent.

**How:**
- Generate a random session token at server start (`secrets.token_urlsafe(32)`), embed it into `DASHBOARD_HTML` when serving `/`, and require it in an `X-Monitor-Token` header on every POST. Reject mismatches with `403`.
- Validate the `Host` header on every request: only accept `127.0.0.1[:port]` and `localhost[:port]` to defeat DNS-rebinding attacks:

```python
def _host_allowed(self) -> bool:
    host = (self.headers.get("Host") or "").split(":")[0]
    return host in ("127.0.0.1", "localhost", "[::1]")
```

- Optionally require the token for GET endpoints that expose status/terminal snapshots as well.

### 2. Harden state-directory permissions (answer.txt is a control channel)

**What:** Files are written with `chmod 0o600`, but directories under `/tmp/terminal-monitor/` are created with default permissions (`0o755`). On shared machines, other local users can read `status.json`, `terminal-snapshot.txt`, and `monitor.log`. Worse, `answer.txt` is a **command injection channel**: anything written there is dispatched to the agent terminal — the directory must never be writable by others.

**How:**
- Create all state directories with mode `0o700`:

```python
os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
os.chmod(self.state_dir, 0o700)  # exist_ok path may keep old perms
```

- In `resolve_project_state_dir()` and `MonitorWebServer.do_POST` (`mkdir(parents=True)`), pass `mode=0o700` too.
- On startup, verify the state dir is owned by the current UID (`os.stat().st_uid == os.getuid()`) and fail closed otherwise — this prevents a pre-created attacker-owned directory in `/tmp`.
- Consider defaulting the state root to `~/.cache/terminal-monitor` (per-user by construction) instead of world-shared `/tmp`.

### 3. Make the monitor lock acquisition atomic

**What:** `_claim_monitor_lock()` does read-then-write (check PID, then `_atomic_json_write`). Two monitors starting simultaneously can both pass the stale-check and both claim the lock (TOCTOU race).

**How:** Use `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)` for the initial claim; only fall back to the stale-PID check (and `os.replace`) when `FileExistsError` is raised. This keeps the current stale-lock recovery while making the happy path race-free.

### 4. Restrict log-file sniffing in `step()` to the project directory

**What:** `step()` extracts any `/*.log` path from descendant process command lines and reads up to 20 KB from it (`extract_test_progress`). A crafted command line could make the monitor read arbitrary files readable by the user (partial info leak into status JSON).

**How:** Only read log files that resolve inside `config.project_dir` (or the state dir):

```python
resolved = Path(log_file).resolve()
if not str(resolved).startswith(str(Path(self.config.project_dir).resolve()) + os.sep):
    continue
```

### 5. Replace CSP `'unsafe-inline'` in the dashboard

**What:** `_reply()` sends `Content-Security-Policy: ... style-src 'unsafe-inline'; script-src 'unsafe-inline'`, which neutralizes most of the CSP benefit for the dashboard page.

**How:** Serve the dashboard JS/CSS from dedicated paths (`/app.js`, `/app.css`) with correct MIME types, or add a per-response nonce (`script-src 'nonce-<random>'`) injected into `DASHBOARD_HTML` at render time.

---

## 🟠 High Priority — Architecture & Maintainability

### 6. Split the 5,000-line monolith into a package

**What:** `terminal_monitor.py` mixes 10+ concerns in one file: safety rules, state persistence, process inspection, GitHub/PR lifecycle, terminal backends, git helpers, classification engine, web server + inline HTML, the monitor engine, and the CLI. This makes navigation, review, and targeted testing hard.

**How:** Convert to a package while keeping the same public API and console entry point:

```
terminal_monitor/
├── __init__.py        # re-export TerminalMonitor, MonitorConfig, AgentProfile, __version__
├── safety.py          # UNSAFE_PHRASES, classify_action_risk, PolicyEnvelope
├── state.py           # TaskState, AttemptLedger, SessionTracker, atomic writes
├── processes.py       # collect_process_activity, interrupt_process_tree, loop guard
├── github.py          # PR state machine, merge gate, CI classification, final report
├── backends.py        # BaseTerminalBackend + Terminal.app / iTerm2 / tmux
├── gitinfo.py         # GitStatus, get_git_status, smart nudges
├── classify.py        # classify_state, extract_todo_progress, question/option parsing
├── web.py             # MonitorWebServer + dashboard assets
├── monitor.py         # TerminalMonitor engine
└── cli.py             # build_parser, config_from_args, main
```

Update `pyproject.toml` from `py-modules` to `packages = ["terminal_monitor"]`. Keep `terminal_monitor.py` as a thin shim (`from terminal_monitor.cli import main`) for one release to preserve the documented `python3 terminal_monitor.py` usage, or update docs in the same PR. Zero-dependency remains true — this only changes file layout.

### 7. Decompose `TerminalMonitor.step()` (~430 lines) into focused handlers

**What:** `step()` performs: stop-file check, PID liveness, tab capture, classification, queued-attempt gating, PR/CI sync, branch safety, loop guard, status export, manual answers, completion, mode switch, and payload decision — in one method. Cyclomatic complexity is very high; adding a rule means editing the middle of a giant function.

**How:** Extract each concern into a private method returning `tuple[int | None, str] | None` (a non-`None` value short-circuits), then run them as an ordered chain:

```python
HANDLERS = (
    self._check_stop_file, self._check_process_gone, self._check_queued_attempts,
    self._sync_pr_stage, self._check_branch_safety, self._check_loop_guard,
    self._handle_manual_answer, self._handle_completion, self._handle_mode_switch,
    self._handle_prompt_decision,
)
for handler in HANDLERS:
    result = handler(ctx)
    if result is not None:
        return result
```

Introduce a small `StepContext` dataclass (pids, tab, history, state, mode, activity, git_status, snapshot) so handlers don't re-fetch data. The same applies to `TerminalMonitor.__init__` (~115 lines): extract `_init_paths()`, `_init_task_state()`, `_init_trackers()`.

### 8. Send-attempt logic is duplicated 6+ times — extract one helper

**What:** The sequence *queue attempt → dry-run check → backend.send → transition sent → transition accepted/ignored → bump counters → log* is copy-pasted in `_recover_agent_loop`, branch-safety nudge, manual answer, final verification, mode switch, and the main prompt path — with slightly different bookkeeping in each (a bug already visible: some paths update `last_change`, others don't).

**How:**

```python
def _dispatch(self, reason: str, payload: str, state: str, *, use_key: bool = False) -> tuple[bool, str, str]:
    attempt_id = self._queue_attempt(reason, payload, state)
    if self.config.dry_run:
        self._transition_attempt(attempt_id, "ignored", detail="dry_run", observed_state=state)
        return False, attempt_id, "dry_run"
    send = self.backend.send_key if use_key else self.backend.send
    ok, detail = send(self.config.process, self.config.title, payload)
    self._transition_attempt(attempt_id, "sent", detail=detail, observed_state=state)
    self._transition_attempt(attempt_id, "accepted" if ok else "ignored", detail=detail, observed_state=state)
    if ok:
        self.sends += 1
        self.last_send = self.last_change = time.monotonic()
    self.log(f"SEND kind={reason} n={self.sends} ok={ok} detail={detail}")
    return ok, attempt_id, detail
```

This guarantees identical ledger/cooldown semantics everywhere and shrinks `step()` substantially.

### 9. Single-source the version number

**What:** The version exists in two places: `__version__ = "1.1.0"` in the module and `version = "1.1.0"` in `pyproject.toml`. They will drift.

**How:** In `pyproject.toml` use dynamic versioning:

```toml
[project]
dynamic = ["version"]

[tool.setuptools.dynamic]
version = { attr = "terminal_monitor.__version__" }
```

Add a regression test asserting `terminal_monitor.__version__` matches the installed metadata when available.

### 10. Restructure `redact_sensitive()` — it introspects its own regex source

**What:** The function decides substitution behavior by checking `pattern.pattern.startswith("(?i)(\\b")` and `"Bearer" in pattern.pattern` — string-matching against its own regex source code. Adding a new pattern silently picks an arbitrary branch; this is fragile and hard to review.

**How:** Pair each pattern with an explicit replacement:

```python
SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(\b(?:authorization|api[_-]?key|...)\b\s*[:=]\s*)[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1<redacted>"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_\-]{12,}\b"), "<redacted>"),
    (re.compile(r"(?i)([?&](?:token|key|secret|password|signature)=)[^&\s]+"), r"\1<redacted>"),
)

def redact_sensitive(text: str) -> str:
    for pattern, repl in SENSITIVE_PATTERNS:
        text = pattern.sub(repl, text)
    return text
```

Also consider adding patterns for AWS keys (`AKIA[0-9A-Z]{16}`), Slack tokens (`xox[baprs]-`), and generic 40+ char hex secrets.

---

## 🟡 Medium Priority — Performance & Robustness

### 11. Split network-backed data out of `get_git_status()` caching

**What:** `_get_git_status_uncached()` runs 5 git subprocesses **plus** `gh pr list` (a network call). `export_status_json()` refreshes it with `ttl_seconds=5.0`, so under active supervision the monitor can hit the GitHub API every ~5 seconds, risking rate limits and adding latency spikes to the poll loop.

**How:** Cache the local git fields and the `open_prs` count with independent TTLs — e.g. 5 s for local `git` data, 60 s for `gh pr list`. Simplest approach: keep two caches keyed by repo path, and have `GitStatus` assembled from both.

### 12. Make the git-status cache thread-safe

**What:** `_GIT_STATUS_CACHE` is a module-level dict mutated from the monitor loop while web-server threads may also trigger reads in future refactors. Dict assignment is atomic in CPython, but the check-then-set pattern can duplicate expensive refreshes and is not safe under free-threaded Python (3.13+ `--disable-gil` builds).

**How:** Guard it with a `threading.Lock`, or replace with a small `functools`-style TTL memoizer object holding its own lock.

### 13. Fix the SSE stream: hard 300-iteration cap and per-second full-file re-reads

**What:** `/api/stream` loops exactly 300 times (~5 min) then silently ends — the dashboard dies quietly unless the client reconnects. On every iteration it re-reads the **entire** log file and re-renders the last 400 lines even when only the status hash is compared, wasting I/O.

**How:**
- Replace `for _ in range(300)` with a loop bounded by client disconnect (write failure) plus a server-shutdown `threading.Event`; or keep a cap but send an explicit `event: reset` message so the client JS knows to reconnect.
- Track `os.stat(log_path).st_mtime_ns`/size and only re-read the log when it changed; include the log tail in the change-hash instead of only `status.json`.
- Catch `BrokenPipeError` in `_reply()` too (currently only in the stream path), so dashboard refreshes never log handler tracebacks.

### 14. Reduce per-line syscall overhead in `append_log()`

**What:** Every log line performs `mkdir`, `stat`, `open`, `write`, `close`, and `chmod`. Under a 3-second poll loop with several `EVENT`/`SEND` lines this is dozens of needless syscalls per minute; `chmod` on every append is redundant after the first.

**How:** Ensure directory + permissions once at monitor start; use `os.open(..., os.O_APPEND | os.O_CREAT, 0o600)` so the mode applies at creation without a separate `chmod`. Optionally keep a cached file object invalidated on rotation.

### 15. Restore `PullRequestStateMachine` state via a constructor/restore method

**What:** `TerminalMonitor.__init__` reaches into the machine: `self.pr_machine.stage = self.task_state.last_known_stage`. Direct attribute pokes bypass any invariant the class maintains (e.g. `seen_pr_number` consistency with the stage) — the exact class of bug the v1.1.0 changelog fixed.

**How:** Add `PullRequestStateMachine.restore(stage: str, pr_number: int | None) -> None` (validating that the stage is a known value and syncing `seen_pr_number`), and make `stage` a read-only property externally.

### 16. `discover_agent_project_dir` failure should degrade loudly, not silently

**What:** Several silent `except Exception: pass` blocks (e.g. `/api/instances` per-directory parse, notification dispatch, webhook post) intentionally never crash — correct for a supervisor — but they also never leave a trace, which makes field debugging ("why is my webhook not firing?") painful.

**How:** Keep fail-open behavior but add an opt-in debug channel: a `--debug-log <path>` flag (or `TERMINAL_MONITOR_DEBUG=1` env) that routes swallowed exceptions through `append_log(debug_path, f"SUPPRESSED {context}: {exc!r}")`. Zero cost when disabled.

---

## 🟢 Lower Priority — Code Quality, Typing, Testing, CI

### 17. Replace `dict[str, Any]` plumbing with `TypedDict`/dataclasses

**What:** Core payloads — tab results (`{"ok", "hist", "busy", "win", "tab", ...}`), merge-gate results, check classifications, attempt records, status JSON — are untyped dicts. Typos in keys (`"headRefOid"` vs `"head"`) can only be caught at runtime.

**How:** Define `TypedDict`s in one `types.py` module (`TabResult`, `MergeGateResult`, `CheckClassification`, `AttemptRecord`) and annotate the producer/consumer functions. Then add `mypy --strict` (or `pyright`) to CI; the stdlib-only codebase makes this cheap.

### 18. Pin lint tooling in CI and add coverage reporting

**What:** CI runs `pip install ruff` unpinned — a new ruff release with new default rules can break every PR overnight. Tests run via `unittest discover` with no coverage signal even though the project already has pytest available locally.

**How:**
- Pin: `pip install "ruff==0.6.*"` (or add a `requirements-dev.txt` / `[dependency-groups] dev`).
- Run tests with coverage: `pip install coverage && coverage run -m unittest discover -s tests && coverage report --fail-under=75`. Upload HTML artifacts on failure.
- Add `ruff format --check` (or keep style rules) so formatting drift is caught too.

### 19. Extract magic numbers into named constants / config

**What:** Behavioral tuning values are scattered inline: `4.0` (fast prompt threshold), `45.0` (protected-branch nudge window), `300` (SSE iterations), `400` (log tail lines), `0.5` (mode-switch sleep), `20000` (log sniff bytes), `6000` (snapshot chars). They are invisible to users and untestable as policies.

**How:** Promote them to module-level constants (`PROMPT_FAST_THRESHOLD_SECONDS = 4.0`, `PROTECTED_BRANCH_NUDGE_WINDOW_SECONDS = 45.0`, ...) and, where operators may legitimately need control (fast threshold, nudge window), add `MonitorConfig` fields + CLI flags, following the existing pattern of `queued_attempt_seconds`.

### 20. Make locale-specific completion patterns profile-configurable

**What:** `TODO_COMPLETION_RATIO_PATTERN`, `TODO_ALL_COMPLETE_PATTERN`, etc. hardcode English **and Portuguese** phrases (`concluídas`, `tarefas`, `não há pendente`). Users of agents replying in other languages (Spanish, German, Chinese) get no completion detection and cannot extend it without editing source.

**How:** Move the pattern strings into `AgentProfile` (with the current EN/PT set as defaults) and allow overriding via the existing custom-profile config file mechanism (`.terminal-monitor.json` → `profiles.<name>.completion_patterns`).

### 21. `now_iso()` lacks sub-second precision for event ordering

**What:** Timestamps are truncated to whole seconds (`%Y-%m-%dT%H:%M:%SZ`). Attempt ledger transitions (`queued → sent → accepted`) routinely happen within the same second, so persisted records cannot be ordered by timestamp alone.

**How:** Include milliseconds: `datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")`. The ledger already keeps monotonic values; this makes the human-readable trail consistent with them.

### 22. Package `supervisor.py` or fold it into the CLI

**What:** `pyproject.toml` ships only `terminal_monitor` (`py-modules = ["terminal_monitor"]`), so `pip install` users don't get `supervisor.py`; it also duplicates arg-handling logic (`sys.path.insert` hack, manual `--prohibition` injection).

**How:** Either add a `terminal-monitor-supervise` console script implementing the same defaults inside the package (preferred), or add `supervisor` to `py-modules`. The defaults it injects (`--auto-allow-permissions`, npm prohibition) could simply become a documented `supervise --defaults opencode-npm-safe` preset.

### 23. Add scenario tests for the web server security surface

**What:** Tests cover the 413 body limit, but not: missing/invalid JSON bodies, the `action=key` path writing `KEY:` files, path traversal attempts in requests, concurrent SSE clients, or the (proposed) token/Host checks.

**How:** Extend the existing web-server test class with: malformed JSON → 500/400 assertion; `Host: evil.example` → 403 (after item 1); token missing → 403; two parallel `/api/stream` readers both receiving an update. These are fast `urllib` tests against an ephemeral port, matching the current test style.

### 24. Document and enforce a public API boundary

**What:** The README markets a "Python SDK" (`TerminalMonitor`, `MonitorConfig`, `AgentProfile`), but the module exports ~100 top-level names with no `__all__`. Consumers may couple to internals (`_explicit_todo_completion`, `_public_status`), making future refactors (item 6) breaking changes.

**How:** Add `__all__ = ["TerminalMonitor", "MonitorConfig", "AgentProfile", "get_profile", "list_profiles", "build_parser", "config_from_args", "main", "__version__"]` and mention SemVer expectations for it in the README. Internal helpers keep the `_` prefix (several already lack it, e.g. `json_safe`, `parse_tab_output` — rename or accept them as public).

---

## Suggested execution order

| Phase | Items | Rationale |
|-------|-------|-----------|
| 1 | 1, 2, 3, 4, 5 | Security fixes: small diffs, high impact, no API change |
| 2 | 8, 10, 15, 21 | Correctness/consistency fixes that reduce bug surface before restructuring |
| 3 | 11, 12, 13, 14 | Performance of the hot poll loop and dashboard |
| 4 | 6, 7, 17, 24 | Structural refactor into a package with typed boundaries |
| 5 | 9, 16, 18, 19, 20, 22, 23 | Tooling, configurability, packaging polish |

All phases should keep the existing 126-test suite green; phases 1–3 are safe to ship as independent PRs.
