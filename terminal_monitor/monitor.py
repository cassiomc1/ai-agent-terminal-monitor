"""The TerminalMonitor supervision engine."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .backends import BaseTerminalBackend, TerminalIdentity, get_backend
from .classify import classify_state, decide_question, extract_todo_progress, infer_current_task_id, redact_snapshot
from .config import DEFAULT_STATE_DIR, MonitorConfig
from .github import (
    FinalVerificationReport,
    PullRequestStateMachine,
    _parent_pid,
    capture_safety_baseline,
    classify_check_result,
    collect_final_evidence,
    evaluate_final_state,
    evaluate_repository_safety,
    get_current_pr_snapshot,
    git_activity_fingerprint,
    retry_infrastructure_checks,
    wait_for_change,
    wait_for_ci_event,
    write_final_report,
)
from .gitinfo import (
    GitStatus,
    discover_agent_project_dir,
    dispatch_webhook,
    ensure_private_dir,
    extract_test_progress,
    generate_smart_nudge,
    get_git_status,
    resolve_project_state_dir,
    send_desktop_notification,
)
from .processes import (
    AgentLoopGuard,
    LoopAssessment,
    ProcessActivity,
    _children_pids,
    assess_agent_commands,
    collect_process_activity,
    interrupt_process_tree,
    pid_is_alive,
    process_is_running,
)
from .profiles import AgentProfile, get_profile
from .safety import UNSAFE_PHRASES, PolicyEnvelope, redact_sensitive
from .state import (
    AttemptLedger,
    SessionTracker,
    TaskState,
    _atomic_json_write,
    _atomic_text_write,
    append_log,
    consume_manual_answer,
    json_safe,
    normalize_snapshot,
    now_iso,
)
from .types import TabResult
from .web import MonitorWebServer

# ---------------------------------------------------------------------------
# Tuning constants (previously inline magic numbers in step())
# ---------------------------------------------------------------------------

# Log-file sniffing cap when extracting test progress from descendant
# command lines, and the bounded snapshot size exported for dashboards.
LOG_SNIFF_MAX_BYTES = 20000
SNAPSHOT_MAX_CHARS = 6000
# Pause between a mode-switch key and the follow-up continuation text.
MODE_SWITCH_SLEEP_SECONDS = 0.5


@dataclass
class StepContext:
    """Per-iteration observation data shared by step handlers.

    Handlers receive one context instead of re-fetching tab state, git
    status, or process activity; a handler returning a non-None result
    short-circuits the ordered chain in :meth:`TerminalMonitor.step`.
    """

    pids: list[int]
    tab: TabResult
    history: str
    snapshot: str
    safe_snapshot: str
    digest: str
    activity: ProcessActivity
    state: str
    mode: str | None
    test_progress: dict[str, Any] | None
    queued_attempt: dict[str, Any] | None = None
    git_status: GitStatus = field(default_factory=GitStatus)
    repository_safety: dict[str, Any] = field(default_factory=dict)
    stable_for: float = 0.0

class TerminalMonitor:
    """Main monitor engine supporting event callbacks, mode management, and step/run lifecycle."""

    def __init__(
        self,
        config: MonitorConfig,
        backend: BaseTerminalBackend | None = None,
        profile: AgentProfile | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or get_backend(config.backend)
        self.profile = profile or get_profile(config.profile or config.process, config.custom_profiles)

        # Merge profile process if config process was default
        if config.process in ("opencode", "agent", "") and self.profile.process != "generic":
            self.config.process = self.profile.process

        # Merge unsafe phrases
        if config.unsafe_phrases:
            self.profile.unsafe_phrases = list(set(self.profile.unsafe_phrases + config.unsafe_phrases))

        # Auto-discover agent project directory if default
        if self.config.project_dir in (".", ""):
            discovered = discover_agent_project_dir(self.backend.get_pids(self.config.process))
            if discovered and get_git_status(discovered, ttl_seconds=0.0).is_repo:
                self.config = replace(self.config, project_dir=discovered)

        self._init_paths()
        self._init_task_state()
        self._init_trackers()

        # Internal state tracking
        self.last_digest = ""
        self.last_change = time.monotonic()
        self.last_seen = time.monotonic()
        self.last_send = 0.0
        self.sends = 0
        self.current_state = "unknown"
        self.current_mode: str | None = None
        self.last_git_fingerprint = ""
        self.monitor_instance_id = uuid4().hex
        self.monitor_started_at = now_iso()
        self.last_heartbeat = self.monitor_started_at
        self.last_action = "initialized"
        self.last_command = ""
        self.last_event_signature = ""
        self._protected_branch_nudged: bool = False
        self._protected_branch_nudge_time: float = 0.0
        self.todo_progress: dict[str, Any] = {"total": 0, "completed": 0, "in_progress": 0, "pending": 0, "items": []}
        self.detected_task_id = ""
        self.web_server: MonitorWebServer | None = None
        self.web_url = ""
        self.last_safe_snapshot = ""
        self._shutdown_requested = False
        self._shutdown_reason = ""
        self._previous_signal_handlers: dict[int, Any] = {}
        self._lock_claimed = False
        self._lifecycle = "stopped"

        self.task_timings: dict[str, dict[str, Any]] = {}
        self.last_notified_attention: str = ""

        # Callbacks
        self.on_state_change: Callable[[str, str], None] | None = None
        self.on_mode_change: Callable[[str | None, str | None], None] | None = None
        self.on_send: Callable[[str, str, bool], None] | None = None
        self.on_attention: Callable[[str, str], None] | None = None
        self.on_complete: Callable[[str], None] | None = None
        self.on_tick: Callable[[str, int], None] | None = None

    def _init_paths(self) -> None:
        """Resolve the state directory and every well-known state file path."""
        # Setup state paths with project-level isolation
        if self.config.state_dir == DEFAULT_STATE_DIR:
            self.state_dir = resolve_project_state_dir(self.config.state_dir, self.config.project_dir)
        else:
            self.state_dir = self.config.state_dir
        ensure_private_dir(self.state_dir)
        self.log_path = os.path.join(self.state_dir, "monitor.log")
        self.attention_path = os.path.join(self.state_dir, "attention.txt")
        self.answer_path = os.path.join(self.state_dir, "answer.txt")
        self.stop_path = os.path.join(self.state_dir, "stop")
        self.monitor_lock_path = os.path.join(self.state_dir, "monitor.pid")
        self.monitor_meta_path = os.path.join(self.state_dir, "monitor.json")
        self.status_json_path = self.config.status_json_path or os.path.join(self.state_dir, "status.json")
        self.terminal_snapshot_path = os.path.join(self.state_dir, "terminal-snapshot.txt")
        self.task_state_path = os.path.join(self.state_dir, "task-state.json")
        self.report_path = self.config.report_path or os.path.join(self.state_dir, "final-report.json")

    def _init_task_state(self) -> None:
        """Merge persisted task state with the configured policy and save it."""
        stored_state = TaskState.load(self.task_state_path)
        current_git_status = get_git_status(self.config.project_dir, ttl_seconds=0.0)
        detected_branch = current_git_status.branch or stored_state.branch
        expected_branch = self.config.expected_branch or stored_state.expected_branch
        if self.config.supervise and not expected_branch:
            expected_branch = detected_branch
        self.task_state = replace(
            stored_state,
            objective=self.config.objective or stored_state.objective,
            prohibitions=tuple(self.config.prohibitions) or stored_state.prohibitions,
            task_id=self.config.task_id or stored_state.task_id,
            required_outcome=self.config.required_outcome or stored_state.required_outcome,
            npm_publish_allowed=self.config.npm_publish_allowed,
            session_id=self.config.session_id or stored_state.session_id,
            branch=detected_branch,
            expected_branch=expected_branch,
            report_path=self.report_path,
        )
        if self.config.supervise and not self.task_state.pr.get("safetyBaselineCaptured"):
            baseline = dict(self.task_state.pr)
            baseline.update(capture_safety_baseline(self.config.project_dir))
            self.task_state = replace(self.task_state, pr=baseline)
        self.task_state.save(self.task_state_path)

    def _init_trackers(self) -> None:
        """Instantiate session, policy, PR-stage, ledger, and loop-guard trackers."""
        self.session_tracker = SessionTracker(
            interaction_history=self.task_state.interaction_marker,
            generation=self.task_state.session_generation,
        )
        self.policy = PolicyEnvelope(self.task_state.objective, self.task_state.prohibitions)
        self.pr_machine = PullRequestStateMachine()
        try:
            self.pr_machine.restore(self.task_state.last_known_stage, self.task_state.pr.get("number"))
        except ValueError:
            self.pr_machine.restore("TASK_RECEIVED")
        self.attempt_ledger = AttemptLedger(list(self.task_state.attempts), max_records=self.config.attempt_history_limit)
        self.agent_loop_guard = AgentLoopGuard(self.config.loop_repeat_limit)
        self.loop_assessment = LoopAssessment()

    def _update_task_timings(self, todo: dict[str, Any]) -> dict[str, Any]:
        """Track per-task start, completion, duration and plan velocity."""
        items = [dict(it) for it in todo.get("items", [])]
        completed_durations: list[float] = []
        for item in items:
            lbl = str(item.get("label", ""))
            st = str(item.get("state", "pending"))
            if not lbl:
                continue
            if st == "in_progress":
                if lbl not in self.task_timings:
                    self.task_timings[lbl] = {"started_at": now_iso(), "start_mono": time.monotonic(), "completed_at": None, "duration_seconds": 0.0}
                else:
                    self.task_timings[lbl]["duration_seconds"] = round(time.monotonic() - float(self.task_timings[lbl].get("start_mono", time.monotonic())), 1)
            elif st == "completed":
                if lbl not in self.task_timings:
                    self.task_timings[lbl] = {"started_at": now_iso(), "start_mono": time.monotonic(), "completed_at": now_iso(), "duration_seconds": 0.0}
                elif not self.task_timings[lbl].get("completed_at"):
                    self.task_timings[lbl]["completed_at"] = now_iso()
                    self.task_timings[lbl]["duration_seconds"] = round(time.monotonic() - float(self.task_timings[lbl].get("start_mono", time.monotonic())), 1)

            timing = self.task_timings.get(lbl, {})
            dur = float(timing.get("duration_seconds", 0.0))
            item["duration_seconds"] = dur
            if dur >= 60.0:
                mins = int(dur // 60)
                secs = int(dur % 60)
                item["duration_formatted"] = f"{mins}m {secs}s"
            elif dur > 0.0:
                item["duration_formatted"] = f"{int(dur)}s"
            else:
                item["duration_formatted"] = ""
            if timing.get("completed_at") and dur > 0.0:
                completed_durations.append(dur)

        avg_dur = round(sum(completed_durations) / len(completed_durations), 1) if completed_durations else 0.0
        pending_count = int(todo.get("pending", 0)) + int(todo.get("in_progress", 0))
        eta_seconds = round(pending_count * avg_dur, 1) if avg_dur > 0.0 else 0.0
        updated = dict(todo)
        updated["avg_duration_seconds"] = avg_dur
        updated["eta_seconds"] = eta_seconds
        if eta_seconds >= 60.0:
            updated["eta_formatted"] = f"~{int(eta_seconds // 60)}m"
        elif eta_seconds > 0.0:
            updated["eta_formatted"] = f"~{int(eta_seconds)}s"
        else:
            updated["eta_formatted"] = ""
        updated["items"] = items
        return updated

    def _notify(self, title: str, message: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Send notifications to desktop and configured webhooks."""
        if self.config.desktop_notifications:
            send_desktop_notification(title, message)
        if self.config.webhook_url:
            dispatch_webhook(self.config.webhook_url, event_type, payload or {"message": message})

    def _monitor_metadata(self, lifecycle: str = "running") -> dict[str, Any]:
        """Build the durable monitor identity used by status/stop/resume commands."""
        return {
            "pid": os.getpid(),
            "instance_id": self.monitor_instance_id,
            "lifecycle": lifecycle,
            "started_at": self.monitor_started_at,
            "heartbeat": self.last_heartbeat,
            "state_dir": str(Path(self.state_dir).resolve()),
            "project_dir": str(Path(self.config.project_dir).resolve()),
            "command": list(self.config.launch_command),
        }

    def _write_monitor_metadata(self, lifecycle: str = "running") -> None:
        _atomic_json_write(self.monitor_meta_path, self._monitor_metadata(lifecycle))

    def _claim_monitor_lock(self) -> bool:
        """Claim one supervisor per state directory, replacing only a stale lock.

        The happy path is race-free: ``O_CREAT | O_EXCL`` creates the lock
        atomically, so two monitors starting simultaneously cannot both pass
        a read-then-write stale check (TOCTOU).  Only when the lock already
        exists do we fall back to stale-PID recovery via ``os.replace``.
        """
        payload = json.dumps({"pid": os.getpid(), "instance_id": self.monitor_instance_id, "started_at": self.monitor_started_at}).encode("utf-8")
        try:
            descriptor = os.open(self.monitor_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Stale-lock recovery: only reached when the lock already exists.
            try:
                existing = json.loads(Path(self.monitor_lock_path).read_text(encoding="utf-8"))
                existing_pid = existing.get("pid") if isinstance(existing, dict) else None
                if existing_pid and int(existing_pid) != os.getpid() and pid_is_alive(existing_pid):
                    return False
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            # Atomic replace so a concurrent claimer can never observe a
            # half-written lock file.
            _atomic_json_write(self.monitor_lock_path, {"pid": os.getpid(), "instance_id": self.monitor_instance_id, "started_at": self.monitor_started_at})
            self._write_monitor_metadata("running")
            self._lock_claimed = True
            self._lifecycle = "running"
            return True
        except OSError as exc:
            self.log(f"LOCK_FAILED error={redact_sensitive(str(exc))}")
            return False
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
        except OSError:
            pass
        self._write_monitor_metadata("running")
        self._lock_claimed = True
        self._lifecycle = "running"
        return True

    def _release_monitor_lock(self, lifecycle: str = "stopped") -> None:
        try:
            lock = json.loads(Path(self.monitor_lock_path).read_text(encoding="utf-8"))
            if int(lock.get("pid", 0)) == os.getpid():
                Path(self.monitor_lock_path).unlink()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        self.last_heartbeat = now_iso()
        with contextlib.suppress(Exception):
            self._write_monitor_metadata(lifecycle)
        self._lock_claimed = False
        self._lifecycle = lifecycle

    def _request_shutdown(self, signum: int, _frame: Any) -> None:
        self._shutdown_requested = True
        self._shutdown_reason = signal.Signals(signum).name

    def _install_shutdown_handlers(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._request_shutdown)

    def _restore_shutdown_handlers(self) -> None:
        for signum, handler in self._previous_signal_handlers.items():
            with contextlib.suppress(ValueError):
                signal.signal(signum, handler)
        self._previous_signal_handlers.clear()

    def _stop_status(self, reason: str) -> None:
        self.last_action = f"stopped:{reason}"
        self.last_heartbeat = now_iso()
        self.export_status_json([], "stopped", {"running": False, "lifecycle": "stopped", "stop_reason": reason})
        self._release_monitor_lock("stopped")

    def _persist_attempt_state(self, *, attempt_id: str = "", prompt: str = "") -> None:
        self.task_state = replace(
            self.task_state,
            attempts=tuple(self.attempt_ledger.records),
            last_attempt_id=attempt_id or self.task_state.last_attempt_id,
            last_prompt=prompt or self.task_state.last_prompt,
        )
        self.task_state.save(self.task_state_path)

    def _queue_attempt(self, reason: str, payload: str, observed_state: str) -> str:
        attempt_id = self.attempt_ledger.queue(reason, payload, observed_state=observed_state)
        self._persist_attempt_state(attempt_id=attempt_id, prompt=payload)
        return attempt_id

    def _transition_attempt(self, attempt_id: str, status: str, *, detail: str = "", observed_state: str = "") -> None:
        self.attempt_ledger.transition(attempt_id, status, detail=detail, observed_state=observed_state)
        self._persist_attempt_state(attempt_id=attempt_id)

    def _record_policy_decision(self, action: str, decision: str, reason: str) -> None:
        decisions = [*self.task_state.policy_decisions, {"timestamp": now_iso(), "action": action, "decision": decision, "reason": reason}]
        self.task_state = replace(self.task_state, policy_decisions=tuple(decisions[-self.config.attempt_history_limit :]))
        self.task_state.save(self.task_state_path)

    def _record_ci_events(self, classifications: Sequence[Mapping[str, Any]]) -> None:
        if not classifications:
            return
        events = [*self.task_state.ci_events]
        for item in classifications:
            fingerprint = f"{item.get('name')}:{item.get('category')}:{item.get('conclusion')}"
            if not any(event.get("fingerprint") == fingerprint for event in events[-len(classifications) :]):
                events.append({"timestamp": now_iso(), "fingerprint": fingerprint, **item})
        self.task_state = replace(self.task_state, ci_events=tuple(events[-self.config.attempt_history_limit :]))
        self.task_state.save(self.task_state_path)

    def _complete_observed_attempt(self, history: str, observed_state: str) -> None:
        """Close the latest accepted attempt only after new terminal output appears."""
        attempt_id = self.task_state.last_attempt_id
        if not attempt_id or self.attempt_ledger.latest(attempt_id or None) is None:
            return
        latest = self.attempt_ledger.latest(attempt_id)
        segment = self.session_tracker.current_segment(history).strip()
        if latest and latest.get("status") in {"accepted", "sent"} and re.search(r"(?im)^\s*QUEUED\s*$", segment):
            self._transition_attempt(
                attempt_id,
                "queued",
                detail="terminal reports message queued",
                observed_state=observed_state,
            )
        elif latest and latest.get("status") == "accepted" and segment:
            self._transition_attempt(
                attempt_id,
                "completed",
                detail="post-send terminal output observed",
                observed_state=observed_state,
            )

    def _stale_queued_attempt(self) -> dict[str, Any] | None:
        latest = self.attempt_ledger.latest(self.task_state.last_attempt_id or None)
        if not latest or latest.get("status") != "queued" or latest.get("detail") != "terminal reports message queued":
            return None
        started_value = latest.get("monotonic")
        age = 0.0
        try:
            started = float(started_value)  # type: ignore[arg-type]
            monotonic_now = time.monotonic()
            if started <= monotonic_now:
                age = max(0.0, monotonic_now - started)
            else:
                raise ValueError("persisted monotonic clock is from a newer boot")
        except (TypeError, ValueError):
            timestamp = str(latest.get("timestamp", ""))
            with contextlib.suppress(ValueError):
                created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                age = max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
        if age < max(0.0, self.config.queued_attempt_seconds):
            return None
        return {"attempt_id": latest.get("attempt_id"), "age_seconds": round(age, 1), "detail": latest.get("detail")}

    def _recover_agent_loop(self, root_pids: list[int], activity: ProcessActivity, observed_state: str) -> tuple[bool, str]:
        """Stop only expensive descendant trees and guide the existing agent session."""
        targets = tuple(pid for pid in activity.expensive_roots if pid not in set(root_pids))
        if not targets:
            return False, "no_verified_expensive_child"
        interrupted = [
            pid
            for pid in targets
            if interrupt_process_tree(set(root_pids), pid, parent_of=_parent_pid, children_of=_children_pids)
        ]
        if not interrupted:
            return False, "no_verified_expensive_child"
        protected_roots = set(root_pids)
        descendants = [pid for pid in dict.fromkeys((*activity.descendants, *targets)) if pid not in protected_roots]
        deadline = time.monotonic() + max(0.0, self.config.loop_interrupt_wait_seconds)
        while any(process_is_running(pid) for pid in descendants) and time.monotonic() < deadline:
            time.sleep(0.05)
        if any(process_is_running(pid) for pid in descendants):
            for target in targets:
                interrupt_process_tree(set(root_pids), target, parent_of=_parent_pid, children_of=_children_pids, sig=signal.SIGTERM)
            deadline = time.monotonic() + max(0.0, self.config.loop_interrupt_wait_seconds)
            while any(process_is_running(pid) for pid in descendants) and time.monotonic() < deadline:
                time.sleep(0.05)
        if any(process_is_running(pid) for pid in descendants):
            return False, "child_tree_did_not_stop"
        if self.sends >= self.config.max_sends:
            return False, "max_sends_reached"
        reason = self.loop_assessment.reason
        evidence = ", ".join(self.loop_assessment.evidence) or "repeated command"
        instruction = (
            f"The monitor detected {reason} ({evidence}) and interrupted only the duplicated/stuck child command tree. "
            "Keep this agent session alive. Diagnose the cause, use targeted checks first, and do not relaunch the same full suite until Git or task progress changes."
        )
        payload = self.policy.compose(instruction, self.task_state.last_known_stage)
        ok, _attempt_id, _detail = self._dispatch("loop_recovery", payload, observed_state)
        if not ok:
            return False, "recovery_prompt_failed"
        self.agent_loop_guard.reset()
        self.last_action = "loop_recovery:accepted"
        return True, f"interrupted={','.join(str(pid) for pid in interrupted)}"

    def _write_terminal_snapshot(self, snapshot: str) -> None:
        self.last_safe_snapshot = snapshot
        with contextlib.suppress(OSError):
            _atomic_text_write(self.terminal_snapshot_path, snapshot)

    def _effective_threshold(self, state: str) -> float:
        """Idle seconds to wait before acting; actionable prompts act faster."""
        if self.config.idle_seconds == 0.0:
            return 0.0
        return self.config.prompt_fast_threshold_seconds if state in ("permission", "question") else self.config.idle_seconds

    def _dispatch(self, reason: str, payload: str, state: str, *, use_key: bool = False) -> tuple[bool, str, str]:
        """Send a continuation through the single queue->send->ledger path.

        Every outbound instruction (manual answer, nudges, loop recovery,
        final verification, mode switch, and the main prompt) funnels
        through here so attempt-ledger, cooldown, and logging semantics are
        identical everywhere instead of duplicated with drift.
        """
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

    def log(self, message: str) -> None:
        append_log(self.log_path, message)

    def export_status_json(self, pids: list[int], state: str, extra: dict[str, Any] | None = None) -> None:
        """Export live structured status for IDEs, dashboards, or subagents."""
        if not self.status_json_path:
            return
        self.last_heartbeat = now_iso()
        # Status export is polled frequently; avoid a GitHub query on every tick.
        git_status = get_git_status(self.config.project_dir, ttl_seconds=5.0)
        data: dict[str, Any] = {
            "schema_version": 2,
            "running": True,
            "lifecycle": "running",
            "monitor_pid": os.getpid(),
            "monitor_instance_id": self.monitor_instance_id,
            "started_at": self.monitor_started_at,
            "heartbeat": self.last_heartbeat,
            "pids": pids,
            "process": self.config.process,
            "profile": self.profile.name,
            "state": state,
            "mode": self.current_mode,
            "last_action": self.last_action,
            "last_command": redact_sensitive(self.last_command),
            "sends": self.sends,
            "stable_seconds": round(time.monotonic() - self.last_change, 1),
            "git": {
                "branch": git_status.branch,
                "head": git_status.head,
                "dirty": git_status.dirty,
                "modified": git_status.modified_count,
                "untracked": git_status.untracked_count,
                "modified_files": list(git_status.modified_files),
                "open_prs": git_status.open_prs_count,
                "last_commit": git_status.last_commit,
            },
            "timestamp": now_iso(),
            "web_url": self.web_url,
            "todo": self.todo_progress,
            "history": {"available": True, "redacted": True, "max_chars": SNAPSHOT_MAX_CHARS},
        }
        if extra:
            data.update(extra)
        task_view = dict(data.get("task") or {})
        task_view.setdefault("task_id", self.task_state.task_id)
        task_view.setdefault("detected_id", self.detected_task_id)
        task_view.setdefault("stage", self.task_state.last_known_stage)
        task_view.setdefault("npm_publish_allowed", self.task_state.npm_publish_allowed)
        data["task"] = task_view
        data["attempts"] = list(self.task_state.attempts[-self.config.attempt_history_limit :])
        data["ci_events"] = list(self.task_state.ci_events[-self.config.attempt_history_limit :])
        data["policy_decisions"] = list(self.task_state.policy_decisions[-self.config.attempt_history_limit :])
        data["report_path"] = self.report_path
        data["prohibitions"] = list(self.task_state.prohibitions)
        data["npm_publish_allowed"] = self.task_state.npm_publish_allowed
        with contextlib.suppress(Exception):
            _atomic_json_write(self.status_json_path, data)

    def inspect(self) -> dict[str, Any]:
        """Query current tab state and return structured status snapshot."""
        pids = self.backend.get_pids(self.config.process)
        tab = self._get_tab(pids)
        if not tab.get("ok"):
            return {
                "ok": False,
                "error": tab.get("error"),
                "pids": pids,
                "state": "missing",
            }

        history = str(tab.get("hist", ""))
        snapshot = normalize_snapshot(history)
        activity = self._process_activity(pids, bool(tab.get("busy")))
        state = classify_state(history, self.profile, activity=activity, session_tracker=self.session_tracker)
        self._complete_observed_attempt(history, state)
        mode = self.profile.detect_mode(history)
        self.last_heartbeat = now_iso()
        if activity.commands:
            self.last_command = activity.commands[0]
        todo_history = self.session_tracker.current_segment(history) if self.session_tracker.interaction_history else history
        if history.strip():
            self.todo_progress = extract_todo_progress(history, session_history=todo_history, profile=self.profile)
        self.detected_task_id = infer_current_task_id(history)
        self.last_action = f"observe:{state}"
        safe_snapshot, snapshot_truncated = redact_snapshot(snapshot)
        safe_tab = dict(tab)
        safe_tab["hist"], _ = redact_snapshot(str(tab.get("hist", "")))
        todo = self.todo_progress
        detected_task_id = infer_current_task_id(history)
        return {
            "ok": True,
            "pids": pids,
            "state": state,
            "mode": mode,
            "snapshot": safe_snapshot,
            "snapshot_truncated": snapshot_truncated,
            "activity": json_safe(activity),
            "todo": todo,
            "task": {"task_id": self.task_state.task_id, "detected_id": detected_task_id, "stage": self.task_state.last_known_stage},
            "tab": safe_tab,
        }

    def _sniffable_log_paths(self, commands: tuple[str, ...]) -> list[str]:
        """Return descendant-command log paths that are safe to read.

        Only files resolving inside the monitored project directory or the
        state directory may be sniffed for test progress: a crafted command
        line must not make the monitor read arbitrary user-readable files
        and leak a summary into the public status JSON.
        """
        allowed: list[Path] = []
        for root in (self.config.project_dir, self.state_dir):
            try:
                allowed.append(Path(root).resolve())
            except OSError:
                continue
        found: list[str] = []
        seen: set[str] = set()
        for cmd in commands:
            for log_file in re.findall(r"(/[^\s'\"]+\.log)", cmd):
                try:
                    resolved = Path(log_file).resolve()
                except OSError:
                    continue
                key = str(resolved)
                inside = any(resolved == root or key.startswith(str(root) + os.sep) for root in allowed)
                if inside and key not in seen:
                    seen.add(key)
                    found.append(log_file)
        return found

    def _observe(self, pids: list[int], tab: TabResult) -> StepContext:
        """Capture tab, activity, classification, and test progress for this iteration."""
        history = str(tab.get("hist", ""))
        snapshot = normalize_snapshot(history)
        safe_snapshot, _ = redact_snapshot(snapshot)
        self._write_terminal_snapshot(safe_snapshot)
        digest = hashlib.sha256(snapshot.encode("utf-8", "replace")).hexdigest()[:16]
        activity = self._process_activity(pids, bool(tab.get("busy")))
        state = classify_state(history, self.profile, activity=activity, session_tracker=self.session_tracker)
        self._complete_observed_attempt(history, state)
        mode = self.profile.detect_mode(history)
        self.last_heartbeat = now_iso()
        if activity.commands:
            self.last_command = activity.commands[0]
        todo_history = self.session_tracker.current_segment(history) if self.session_tracker.interaction_history else history
        if history.strip():
            raw_todo = extract_todo_progress(history, session_history=todo_history, profile=self.profile)
            self.todo_progress = self._update_task_timings(raw_todo)
        self.detected_task_id = infer_current_task_id(history)
        self.last_action = f"observe:{state}"

        test_progress = extract_test_progress(history)
        if not test_progress and activity.commands:
            for log_file in self._sniffable_log_paths(activity.commands):
                if os.path.exists(log_file):
                    with contextlib.suppress(Exception):
                        test_progress = extract_test_progress(Path(log_file).read_text(encoding="utf-8", errors="ignore")[-LOG_SNIFF_MAX_BYTES:])
                        if test_progress:
                            break
        return StepContext(
            pids=pids,
            tab=tab,
            history=history,
            snapshot=snapshot,
            safe_snapshot=safe_snapshot,
            digest=digest,
            activity=activity,
            state=state,
            mode=mode,
            test_progress=test_progress,
        )

    def _check_stop_file(self) -> tuple[int, str] | None:
        if os.path.exists(self.stop_path):
            self._stop_status("stop_file")
            return 0, "CANCELLED"
        return None

    def _check_process_gone(self) -> tuple[int, str] | None:
        pids = self.backend.get_pids(self.config.process)
        if pids:
            self.last_seen = time.monotonic()
            return None
        if time.monotonic() - self.last_seen >= self.config.gone_seconds:
            self._stop_status("agent_gone")
            return 0, "PROCESS_GONE"
        return None

    def _check_queued_attempts(self, ctx: StepContext) -> tuple[int | None, str] | None:
        queued_attempt = self._stale_queued_attempt()
        if not queued_attempt:
            latest_attempt = self.attempt_ledger.latest(self.task_state.last_attempt_id or None)
            if latest_attempt and latest_attempt.get("status") == "queued" and latest_attempt.get("detail") == "terminal reports message queued":
                return None, "WAITING_QUEUED_ATTEMPT"
            return None
        if not self.config.supervise:
            return None, "WAITING_QUEUED_ATTEMPT"
        self._record_policy_decision(str(queued_attempt.get("attempt_id", "")), "attention", "queued_attempt_stale")
        Path(self.attention_path).write_text(json.dumps(queued_attempt, indent=2) + "\n" + ctx.safe_snapshot + "\n", encoding="utf-8")
        self.log(f"PAUSE kind=queued_attempt_stale age={queued_attempt['age_seconds']}")
        self.export_status_json(ctx.pids, "queued_attempt_stale", {"queued_attempt": queued_attempt})
        self._notify("AI Terminal Monitor: Attention Required", "Queued attempt is stale", "attention_required", {"reason": "queued_attempt_stale"})
        return 3, f"ATTENTION_REQUIRED kind=queued_attempt_stale file={self.attention_path}"

    def _sync_pr_stage(self, ctx: StepContext) -> tuple[int | None, str] | None:
        if not self.config.supervise:
            return None
        reference = str(self.task_state.pr.get("number") or self.task_state.branch or "")
        pr_snapshot = get_current_pr_snapshot(self.config.project_dir, reference)
        if not pr_snapshot:
            return None
        classifications = [classify_check_result(check) for check in pr_snapshot.get("statusCheckRollup") or []]
        self._record_ci_events(classifications)
        prev_stage = self.task_state.last_known_stage
        stage = self.pr_machine.advance({
            "number": pr_snapshot.get("number"),
            "state": pr_snapshot.get("state"),
            "checks": pr_snapshot.get("statusCheckRollup") or [],
        })
        if stage == "PR_CREATED" and prev_stage != "PR_CREATED":
            self._notify("AI Terminal Monitor: PR Created", f"PR #{pr_snapshot.get('number')} created", "pr_created", {"pr": pr_snapshot.get("number")})
        metadata = dict(self.task_state.pr)
        metadata.update({
            "number": pr_snapshot.get("number"),
            "head": pr_snapshot.get("headRefOid"),
            "checkClassifications": classifications,
        })
        if stage == "CI_RETRY_REQUIRED":
            retried = retry_infrastructure_checks(self.config.project_dir, pr_snapshot)
            if retried:
                metadata["retriedRuns"] = retried
                stage = "CI_PENDING"
        self.task_state = replace(self.task_state, last_known_stage=stage, pr=metadata)
        self.task_state.save(self.task_state_path)
        return None

    def _refresh_git_context(self, ctx: StepContext) -> tuple[int | None, str] | None:
        ctx.git_status = get_git_status(self.config.project_dir)
        event_signature = "|".join(
            (
                ctx.state,
                str(ctx.mode or ""),
                ",".join(str(pid) for pid in ctx.pids),
                ",".join(str(pid) for pid in ctx.activity.descendants),
                str(self.todo_progress.get("completed", 0)),
                str(self.todo_progress.get("total", 0)),
                ctx.git_status.head,
                ",".join(ctx.activity.duplicate_commands),
            )
        )
        if event_signature != self.last_event_signature:
            self.last_event_signature = event_signature
            self.log(
                f"EVENT state={ctx.state} mode={ctx.mode or 'unknown'} roots={len(ctx.pids)} children={len(ctx.activity.descendants)} "
                f"task={self.todo_progress.get('completed', 0)}/{self.todo_progress.get('total', 0)} git={ctx.git_status.head[:8] or 'none'}"
            )
        if ctx.git_status.branch not in self.config.protected_branches:
            self._protected_branch_nudged = False
            if self.task_state.expected_branch in self.config.protected_branches or not self.task_state.expected_branch:
                self.task_state = replace(self.task_state, expected_branch=ctx.git_status.branch, branch=ctx.git_status.branch)
                self.task_state.save(self.task_state_path)
                self.log(f"BRANCH_TRACK branch={ctx.git_status.branch}")
        return None

    def _check_branch_safety(self, ctx: StepContext) -> tuple[int | None, str] | None:
        repository_safety = evaluate_repository_safety(
            ctx.git_status,
            expected_branch=self.task_state.expected_branch,
            protected_branches=self.config.protected_branches,
        )
        ctx.repository_safety = repository_safety
        if not (self.config.supervise and not repository_safety["safe"]):
            return None
        reason = str(repository_safety["reason"])
        if (
            reason == "protected_branch_dirty"
            and self.config.smart_nudges
            and not self._protected_branch_nudged
            and not (self.config.expected_branch and self.config.expected_branch != ctx.git_status.branch)
        ):
            self._protected_branch_nudged = True
            self._protected_branch_nudge_time = time.monotonic()
            nudge_msg = (
                f"Direct changes detected on protected branch '{ctx.git_status.branch}'. "
                f"Please create and switch to a dedicated feature branch (e.g. 'git checkout -b <feature-name>') "
                f"and commit your changes before proceeding."
            )
            self._dispatch("branch_safety", nudge_msg, ctx.state)
            self.log(f"NUDGE kind=protected_branch_dirty branch={ctx.git_status.branch}")
            return None, "NUDGE_SENT kind=protected_branch_dirty"
        if (
            reason == "protected_branch_dirty"
            and self._protected_branch_nudged
            and (time.monotonic() - self._protected_branch_nudge_time < self.config.protected_branch_nudge_window_seconds)
            and not (self.config.expected_branch and self.config.expected_branch != ctx.git_status.branch)
        ):
            return None
        self._record_policy_decision(
            f"branch={repository_safety.get('branch', '')}",
            "attention",
            reason,
        )
        with open(self.attention_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(repository_safety, indent=2) + "\n" + ctx.safe_snapshot + "\n")
        self.log(f"PAUSE kind=repository_safety reason={reason}")
        self.export_status_json(ctx.pids, "branch_safety", {"repository_safety": repository_safety})
        self._notify("AI Terminal Monitor: Attention Required", f"Repository safety violation: {reason}", "attention_required", {"reason": reason})
        return 3, f"ATTENTION_REQUIRED kind=repository_safety reason={reason} file={self.attention_path}"

    def _check_loop_guard(self, ctx: StepContext) -> tuple[int | None, str] | None:
        immediate_assessment = assess_agent_commands(
            ctx.activity.commands,
            duplicate_commands=ctx.activity.duplicate_commands,
            allow_history_rewrite=self.config.allow_history_rewrite,
        )
        progress_fingerprint = f"{self.todo_progress.get('completed', 0)}/{self.todo_progress.get('total', 0)}"
        git_fingerprint = hashlib.sha256(
            f"{ctx.git_status.head}|{ctx.git_status.branch}|{ctx.git_status.modified_files}|{ctx.git_status.untracked_count}".encode("utf-8", "replace")
        ).hexdigest()[:16]
        repeated_assessment = self.agent_loop_guard.observe(
            ctx.digest,
            progress_fingerprint,
            git_fingerprint,
            ctx.git_status.head,
            ctx.activity.commands,
            episode=",".join(str(pid) for pid in ctx.activity.direct_descendants),
        )
        self.loop_assessment = immediate_assessment if immediate_assessment.detected else repeated_assessment
        if not (self.config.supervise and self.config.loop_guard and self.loop_assessment.detected):
            return None
        reason = self.loop_assessment.reason
        attention = {
            "kind": "agent_loop",
            "reason": reason,
            "evidence": list(self.loop_assessment.evidence),
            "occurrences": self.loop_assessment.occurrences,
            "activity": json_safe(ctx.activity),
        }
        self._record_policy_decision("agent_process_activity", "attention", reason)
        if reason in {"duplicate_expensive_commands", "repeated_expensive_command_without_progress"}:
            recovered, recovery_detail = self._recover_agent_loop(ctx.pids, ctx.activity, ctx.state)
            attention["recovery"] = {"ok": recovered, "detail": recovery_detail, "root_pids_protected": list(ctx.pids)}
            if recovered:
                Path(self.attention_path).write_text(json.dumps(attention, indent=2) + "\n", encoding="utf-8")
                self.log(f"RECOVER kind=agent_loop reason={reason} {recovery_detail}")
                self.export_status_json(ctx.pids, "loop_recovered", {"loop_guard": attention})
                return None, f"LOOP_RECOVERED reason={reason} {recovery_detail}"
        Path(self.attention_path).write_text(json.dumps(attention, indent=2) + "\n" + ctx.safe_snapshot + "\n", encoding="utf-8")
        self.log(f"PAUSE kind=agent_loop reason={reason}")
        self.export_status_json(ctx.pids, "agent_loop", {"loop_guard": attention})
        self._notify("AI Terminal Monitor: Loop Detected", f"Monitored agent loop: {reason}", "attention_required", {"reason": reason})
        return 3, f"ATTENTION_REQUIRED kind=agent_loop reason={reason} file={self.attention_path}"

    def _export_observed_status(self, ctx: StepContext) -> tuple[int | None, str] | None:
        self.export_status_json(ctx.pids, ctx.state, {
            "activity": {
                "active": ctx.activity.active,
                "descendants": list(ctx.activity.descendants),
                "direct_descendants": list(ctx.activity.direct_descendants),
                "commands": list(ctx.activity.commands),
                "cpu_percent": ctx.activity.cpu_percent,
                "oldest_seconds": ctx.activity.oldest_seconds,
                "git_changed": ctx.activity.git_changed,
                "duplicate_commands": list(ctx.activity.duplicate_commands),
                "expensive_roots": list(ctx.activity.expensive_roots),
                "test_progress": ctx.test_progress,
            },
            "loop_guard": json_safe(self.loop_assessment),
            "task": {
                "task_id": self.task_state.task_id,
                "stage": self.task_state.last_known_stage,
                "session_generation": self.session_tracker.generation,
                "pr": self.task_state.pr,
                "npm_publish_allowed": self.task_state.npm_publish_allowed,
            },
            "repository_safety": ctx.repository_safety,
        })
        return None

    def _finish_observation(self, ctx: StepContext) -> tuple[int | None, str] | None:
        if self.config.once:
            return 0, f"STATE={ctx.state} MODE={ctx.mode} PID_COUNT={len(ctx.pids)}"
        if ctx.digest != self.last_digest:
            self.last_digest = ctx.digest
            self.last_change = time.monotonic()
        ctx.stable_for = time.monotonic() - self.last_change
        return None

    def _handle_manual_answer(self, ctx: StepContext) -> tuple[int | None, str] | None:
        # A human/operator answer is an explicit instruction and therefore
        # outranks inferred UI state, spinner text, thresholds, and cooldowns.
        manual_answer = consume_manual_answer(self.answer_path)
        if not manual_answer:
            return None
        allowed, policy_reason = self.policy.authorize_action(
            manual_answer,
            unsafe_phrases=tuple(dict.fromkeys([*UNSAFE_PHRASES, *self.config.unsafe_phrases])),
            npm_publish_allowed=self.task_state.npm_publish_allowed,
        )
        if not allowed:
            self._record_policy_decision(manual_answer, "blocked", policy_reason)
            with open(self.attention_path, "w", encoding="utf-8") as handle:
                handle.write(ctx.safe_snapshot + "\n")
            self.log(f"PAUSE kind=policy_conflict reason={policy_reason}")
            return 3, f"ATTENTION_REQUIRED kind=policy_conflict file={self.attention_path}"
        ok, attempt_id, detail = self._dispatch("manual", manual_answer, ctx.state)
        if detail == "dry_run":
            return 0, f"DRY_RUN kind=manual payload={manual_answer}"
        self.export_status_json(ctx.pids, ctx.state, {"last_attempt_id": attempt_id})
        self.last_action = f"send:manual:{'accepted' if ok else 'failed'}"
        self.session_tracker.mark_interaction(ctx.history)
        self.task_state = replace(
            self.task_state,
            session_generation=self.session_tracker.generation,
            interaction_marker=self.session_tracker.interaction_history,
        )
        self.task_state.save(self.task_state_path)
        if self.on_send:
            self.on_send("manual", manual_answer, ok)
        return (None, f"SENT kind=manual n={self.sends}") if ok else (1, f"SEND_FAILED kind=manual n={self.sends}")

    def _handle_completion(self, ctx: StepContext) -> tuple[int | None, str] | None:
        if ctx.state != "completed" or not self.config.completion_check:
            return None
        merge_required = self.config.supervise and self.task_state.required_outcome.lower() == "merged"
        if merge_required and self.task_state.last_known_stage != "POST_MERGE_VERIFY":
            self.log(f"WAIT completion_text_before_required_stage stage={self.task_state.last_known_stage}")
            return None, f"WAITING_FOR_REQUIRED_OUTCOME stage={self.task_state.last_known_stage}"
        if merge_required:
            report = evaluate_final_state(collect_final_evidence(self.config.project_dir, self.task_state))
            if not report.ok:
                if time.monotonic() - self.last_send < self.config.cooldown_seconds:
                    return None, "WAITING_FINAL_VERIFICATION"
                instruction = "Resolve the remaining final-verification checks: " + ", ".join(report.failures)
                payload = self.policy.compose(instruction, "POST_MERGE_VERIFY")
                ok, _attempt_id, detail = self._dispatch("final_verification", payload, ctx.state)
                if detail == "dry_run":
                    return 0, "DRY_RUN kind=final_verification"
                self.last_action = f"send:final_verification:{'accepted' if ok else 'failed'}"
                return (None, "SENT kind=final_verification") if ok else (1, "SEND_FAILED kind=final_verification")
        self.log("SUCCESS: Completion indicators detected. Work complete.")
        if self.config.supervise:
            evidence = collect_final_evidence(self.config.project_dir, self.task_state)
            write_final_report(self.report_path, self.task_state, evidence, FinalVerificationReport(True, {"completion_detected": True}, ()))
        if self.on_complete:
            self.on_complete(ctx.snapshot)
        self.export_status_json(ctx.pids, "completed", {"done": True})
        return 0, "COMPLETED"

    def _handle_wait_threshold(self, ctx: StepContext) -> tuple[int | None, str] | None:
        threshold = self._effective_threshold(ctx.state)
        if ctx.state == "thinking" or ctx.stable_for < threshold:
            if self.on_tick:
                self.on_tick(ctx.state, len(ctx.pids))
            return None, f"WAITING state={ctx.state} stable_for={ctx.stable_for:.1f}"
        if time.monotonic() - self.last_send < self.config.cooldown_seconds:
            return None, "COOLDOWN"
        return None

    def _handle_mode_switch(self, ctx: StepContext) -> tuple[int | None, str] | None:
        mode_threshold = self._effective_threshold("permission")
        if not (self.config.auto_switch_modes and ctx.mode == "plan" and self.profile.is_plan_ready(ctx.history) and ctx.stable_for >= mode_threshold):
            return None
        self.log("MODE: Plan completed. Auto-switching mode via switch key.")
        self.last_action = "mode_switch:queued"
        ok, _attempt_id, detail = self._dispatch("mode_switch", self.profile.mode_switch_key, ctx.state, use_key=True)
        if detail == "dry_run":
            self._record_policy_decision("mode_switch", "ignored", "dry_run")
            return 0, "DRY_RUN kind=mode_switch"
        self.last_action = "mode_switch:accepted" if ok else "mode_switch:failed"
        if self.config.continue_text:
            time.sleep(MODE_SWITCH_SLEEP_SECONDS)
            self.backend.send(self.config.process, self.config.title, self.config.continue_text)
        return None, "MODE_SWITCH_SENT"

    def _handle_prompt_decision(self, ctx: StepContext) -> tuple[int | None, str] | None:
        payload: str | None = self.config.continue_text
        reason = "idle"

        if ctx.state == "permission":
            payload = self.profile.auto_permission_payload if self.config.auto_allow_permissions else None
            reason = "permission"
        elif ctx.state == "question":
            payload = decide_question(ctx.history, self.profile)
            reason = "question"
        elif ctx.state == "idle" and self.config.smart_nudges:
            payload = generate_smart_nudge(ctx.git_status, self.config.continue_text)
            reason = "smart_nudge"

        if payload and (self.policy.objective or self.policy.prohibitions):
            try:
                payload = self.policy.compose(payload, self.task_state.last_known_stage)
            except ValueError as exc:
                self._record_policy_decision(payload, "blocked", str(exc))
                payload = None
                reason = "policy_conflict"

        if payload is None:
            with open(self.attention_path, "w", encoding="utf-8") as handle:
                handle.write(ctx.safe_snapshot + "\n")
            if self.on_attention:
                self.on_attention(reason, ctx.safe_snapshot)
            self.log(f"PAUSE kind={reason} hash={ctx.digest}")
            return 3, f"ATTENTION_REQUIRED kind={reason} file={self.attention_path}"

        allowed, policy_reason = self.policy.authorize_action(
            payload,
            unsafe_phrases=tuple(dict.fromkeys([*UNSAFE_PHRASES, *self.config.unsafe_phrases])),
            npm_publish_allowed=self.task_state.npm_publish_allowed,
        )
        if not allowed:
            self._record_policy_decision(payload, "blocked", policy_reason)
            with open(self.attention_path, "w", encoding="utf-8") as handle:
                handle.write(ctx.safe_snapshot + "\n")
            self.log(f"PAUSE kind=policy_conflict reason={policy_reason}")
            return 3, f"ATTENTION_REQUIRED kind=policy_conflict file={self.attention_path}"

        ok, attempt_id, detail = self._dispatch(reason, payload, ctx.state)
        if detail == "dry_run":
            return 0, f"DRY_RUN kind={reason} payload={payload or '<enter>'}"
        self.export_status_json(ctx.pids, ctx.state, {"last_attempt_id": attempt_id})
        self.last_action = f"send:{reason}:{'accepted' if ok else 'failed'}"
        if ok:
            self.session_tracker.mark_interaction(ctx.history)
            self.task_state = replace(
                self.task_state,
                session_generation=self.session_tracker.generation,
                interaction_marker=self.session_tracker.interaction_history,
            )
            self.task_state.save(self.task_state_path)

        if self.on_send:
            self.on_send(reason, payload, ok)

        if not ok:
            return 1, f"SEND_FAILED kind={reason} n={self.sends}"

        if self.sends >= self.config.max_sends:
            return 0, "MAX_SENDS_REACHED"

        return None, f"SENT kind={reason} n={self.sends}"

    def step(self) -> tuple[int | None, str]:
        """Perform a single monitor iteration.

        Returns (exit_code, status_message).
        If exit_code is None, the monitor should continue running.

        The iteration is an ordered chain of focused handlers over one
        shared :class:`StepContext`; the first non-None handler result
        short-circuits the chain.
        """
        early = self._check_stop_file()
        if early is not None:
            return early
        early = self._check_process_gone()
        if early is not None:
            return early

        pids = self.backend.get_pids(self.config.process)
        if pids:
            self.last_seen = time.monotonic()
        elif time.monotonic() - self.last_seen >= self.config.gone_seconds:
            self._stop_status("agent_gone")
            return 0, "PROCESS_GONE"

        tab = self._get_tab(pids)
        if not tab.get("ok"):
            if self.config.once:
                return 2, f"MISSING: {tab.get('error')}"
            return None, "TAB_MISSING"

        ctx = self._observe(pids, tab)

        if ctx.mode != self.current_mode:
            if self.on_mode_change:
                self.on_mode_change(self.current_mode, ctx.mode)
            self.current_mode = ctx.mode

        if ctx.state != self.current_state:
            if self.on_state_change:
                self.on_state_change(self.current_state, ctx.state)
            self.current_state = ctx.state

        for handler in (
            self._check_queued_attempts,
            self._sync_pr_stage,
            self._refresh_git_context,
            self._check_branch_safety,
            self._check_loop_guard,
            self._export_observed_status,
            self._finish_observation,
            self._handle_manual_answer,
            self._handle_completion,
            self._handle_wait_threshold,
            self._handle_mode_switch,
            self._handle_prompt_decision,
        ):
            result = handler(ctx)
            if result is not None:
                return result
        return None, "CONTINUE"
    def run(self) -> int:
        """Run monitor loop continuously until exit condition is met."""
        if not self._claim_monitor_lock():
            self.log("EXIT code=2 msg=MONITOR_ALREADY_RUNNING")
            return 2
        self.log(f"START process={self.config.process} profile={self.profile.name} backend={self.backend.name()}")
        self._install_shutdown_handlers()
        try:
            if self.config.web_ui and not self.config.once:
                try:
                    self.web_server = MonitorWebServer(
                        self.status_json_path,
                        self.log_path,
                        self.config.web_port,
                        self.terminal_snapshot_path,
                        answer_path=self.answer_path,
                        state_root=str(Path(self.state_dir).parent),
                    )
                    self.web_url = self.web_server.start()
                    self.log(f"WEB_UI url={self.web_url}")
                    if self.config.web_open_browser:
                        with contextlib.suppress(Exception):
                            webbrowser.open(self.web_url)
                except (OSError, ValueError, OverflowError) as exc:
                    self.log(f"WEB_UI_FAILED error={redact_sensitive(str(exc))}")
                    if self.web_server:
                        self.web_server.stop()
                    self.web_server = None
            while True:
                if self._shutdown_requested:
                    reason = self._shutdown_reason or "shutdown"
                    self.log(f"EXIT code=0 msg=STOPPED reason={reason}")
                    self._stop_status(reason)
                    return 0
                code, msg = self.step()
                if code is not None:
                    self.log(f"EXIT code={code} msg={msg}")
                    if msg == "COMPLETED":
                        self.export_status_json([], "completed", {"running": False, "lifecycle": "completed", "done": True})
                        self._release_monitor_lock("completed")
                    elif self._lock_claimed:
                        self._stop_status(msg.lower().replace(" ", "_"))
                    return code
                pr_number = self.task_state.pr.get("number")
                if self.task_state.last_known_stage == "CI_PENDING" and pr_number and shutil.which("gh"):
                    wait_for_ci_event(self.config.project_dir, int(pr_number), timeout_seconds=max(5.0, self.config.poll_seconds))
                    continue
                initial = self._change_fingerprint()
                wait_for_change(
                    self._change_fingerprint,
                    initial,
                    timeout_seconds=self.config.poll_seconds,
                    interval_seconds=min(0.5, self.config.poll_seconds),
                )
        except KeyboardInterrupt:
            self.log("EXIT code=130 msg=INTERRUPTED")
            self._stop_status("SIGINT")
            return 130
        finally:
            if self.web_server:
                self.web_server.stop()
            self._restore_shutdown_handlers()
            if self._lock_claimed:
                self._release_monitor_lock(self._lifecycle if self._lifecycle != "running" else "stopped")

    def _change_fingerprint(self) -> str:
        """Cheap file, process, and repository signals used for early wakeup."""
        files = []
        for path in (self.answer_path, self.stop_path):
            with contextlib.suppress(OSError):
                stat = os.stat(path)
                files.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        pids = ",".join(str(pid) for pid in self.backend.get_pids(self.config.process))
        value = "|".join(files) + pids + git_activity_fingerprint(self.config.project_dir)
        return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()

    def _get_tab(self, pids: list[int]) -> TabResult:
        identity = TerminalIdentity(
            project_path=str(Path(self.config.project_dir).resolve()),
            branch=self.task_state.branch or get_git_status(self.config.project_dir).branch,
            session_id=self.task_state.session_id,
            title=self.config.title or "",
            root_pid=pids[0] if pids else None,
        )
        return self.backend.get_tab_for_identity(self.config.process, identity)

    def _process_activity(self, pids: list[int], terminal_busy: bool = False) -> ProcessActivity:
        activity = collect_process_activity(pids)
        fingerprint = git_activity_fingerprint(self.config.project_dir)
        changed = bool(self.last_git_fingerprint and fingerprint != self.last_git_fingerprint)
        self.last_git_fingerprint = fingerprint
        return replace(activity, active=activity.active or terminal_busy or changed, git_changed=changed)
