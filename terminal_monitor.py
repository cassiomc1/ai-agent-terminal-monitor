#!/usr/bin/env python3
"""Monitor and safely nudge any AI CLI agent running in macOS Terminal.app, iTerm2, or tmux.

Generic and extensible across any agent (Claude Code, OpenCode, Aider, Goose, etc.)
and any project via configuration files, profiles, and customizable rules.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

# Optional TOML support (standard in Python 3.11+)
try:
    import tomllib
except ImportError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None


# ---------------------------------------------------------------------------
# Default Safety and Decision Rules
# ---------------------------------------------------------------------------

UNSAFE_PHRASES: tuple[str, ...] = (
    "bypass",
    "delete",
    "disable validator",
    "discard",
    "drop database",
    "drop table",
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
    "weaken",
)

DEFAULT_PREFERRED_ANSWERS: tuple[str, ...] = (
    "continue",
    "proceed",
    "keep",
    "inspect",
    "validate",
    "fail closed",
    "yes",
    "allow",
    "approve",
)

SPECIAL_KEY_CODES: dict[str, int] = {
    "tab": 9,
    "\t": 9,
    "enter": 13,
    "return": 13,
    "\r": 13,
    "\n": 10,
    "esc": 27,
    "escape": 27,
    "\x1b": 27,
    "ctrl+c": 3,
    "ctrl_c": 3,
    "\x03": 3,
    "ctrl+p": 16,
    "ctrl_p": 16,
    "\x10": 16,
    "ctrl+d": 4,
    "ctrl_d": 4,
    "\x04": 4,
    "backspace": 127,
    "delete": 127,
}


class StateFileError(RuntimeError):
    """Raised when persistent supervisor state cannot be trusted."""


def json_safe(value: Any) -> Any:
    """Convert monitor values into deterministic JSON-compatible structures."""
    if is_dataclass(value):
        return {key: json_safe(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, set):
        return [json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    return value


def _atomic_json_write(path: str | Path, data: dict[str, Any]) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(json_safe(data), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)


@dataclass(frozen=True)
class TaskState:
    """Durable task identity, policy, stage, and PR metadata."""

    objective: str = ""
    prohibitions: tuple[str, ...] = ()
    plan: tuple[str, ...] = ()
    branch: str = ""
    task_id: str = ""
    required_outcome: str = "merged"
    npm_publish_allowed: bool = False
    last_known_stage: str = "TASK_RECEIVED"
    pr: dict[str, Any] = field(default_factory=dict)
    session_generation: int = 0
    session_id: str = ""
    interaction_marker: str = ""
    expected_branch: str = ""
    attempts: tuple[dict[str, Any], ...] = ()
    ci_events: tuple[dict[str, Any], ...] = ()
    policy_decisions: tuple[dict[str, Any], ...] = ()
    last_prompt: str = ""
    last_attempt_id: str = ""
    report_path: str = ""

    def save(self, path: str | Path) -> None:
        data = {
            "objective": self.objective,
            "prohibitions": list(self.prohibitions),
            "plan": list(self.plan),
            "branch": self.branch,
            "taskId": self.task_id,
            "requiredOutcome": self.required_outcome,
            "npmPublishAllowed": self.npm_publish_allowed,
            "lastKnownStage": self.last_known_stage,
            "pr": self.pr,
            "sessionGeneration": self.session_generation,
            "sessionId": self.session_id,
            "interactionMarker": self.interaction_marker,
            "expectedBranch": self.expected_branch,
            "attempts": list(self.attempts),
            "ciEvents": list(self.ci_events),
            "policyDecisions": list(self.policy_decisions),
            "lastPrompt": self.last_prompt,
            "lastAttemptId": self.last_attempt_id,
            "reportPath": self.report_path,
        }
        _atomic_json_write(path, data)

    @classmethod
    def load(cls, path: str | Path) -> TaskState:
        target = Path(path)
        if not target.exists():
            return cls()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("state root must be an object")
            return cls(
                objective=str(data.get("objective", "")),
                prohibitions=tuple(str(item) for item in data.get("prohibitions", [])),
                plan=tuple(str(item) for item in data.get("plan", [])),
                branch=str(data.get("branch", "")),
                task_id=str(data.get("taskId", "")),
                required_outcome=str(data.get("requiredOutcome", "merged")),
                npm_publish_allowed=bool(data.get("npmPublishAllowed", False)),
                last_known_stage=str(data.get("lastKnownStage", "TASK_RECEIVED")),
                pr=dict(data.get("pr", {})),
                session_generation=int(data.get("sessionGeneration", 0)),
                session_id=str(data.get("sessionId", "")),
                interaction_marker=str(data.get("interactionMarker", "")),
                expected_branch=str(data.get("expectedBranch", "")),
                attempts=tuple(dict(item) for item in data.get("attempts", []) if isinstance(item, dict)),
                ci_events=tuple(dict(item) for item in data.get("ciEvents", []) if isinstance(item, dict)),
                policy_decisions=tuple(dict(item) for item in data.get("policyDecisions", []) if isinstance(item, dict)),
                last_prompt=str(data.get("lastPrompt", "")),
                last_attempt_id=str(data.get("lastAttemptId", "")),
                report_path=str(data.get("reportPath", "")),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StateFileError(f"Untrusted task state at {target}: {exc}") from exc


ATTEMPT_STATUSES = {"queued", "sent", "accepted", "completed", "ignored"}


@dataclass
class AttemptLedger:
    """Bounded event ledger for idempotent, observable continuation attempts."""

    records: list[dict[str, Any]] = field(default_factory=list)
    max_records: int = 100
    _sequence: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        sequence_values = []
        for record in self.records:
            attempt_id = str(record.get("attempt_id", ""))
            with contextlib.suppress(ValueError, IndexError):
                sequence_values.append(int(attempt_id.rsplit("-", 1)[1]))
        self._sequence = max(sequence_values, default=0)

    def _append(self, attempt_id: str, status: str, **details: Any) -> None:
        if status not in ATTEMPT_STATUSES:
            raise ValueError(f"Unknown attempt status: {status}")
        record: dict[str, Any] = {
            "attempt_id": attempt_id,
            "status": status,
            "timestamp": now_iso(),
            "monotonic": time.monotonic(),
        }
        record.update({key: value for key, value in details.items() if value is not None})
        self.records.append(record)
        self.records[:] = self.records[-max(1, int(self.max_records)) :]

    def queue(self, reason: str, payload: str, *, observed_state: str = "") -> str:
        self._sequence += 1
        attempt_id = f"attempt-{int(time.time() * 1000)}-{self._sequence}"
        self._append(attempt_id, "queued", reason=reason, payload=payload, observed_state=observed_state)
        return attempt_id

    def transition(self, attempt_id: str, status: str, *, detail: str = "", observed_state: str = "") -> None:
        if not any(record.get("attempt_id") == attempt_id for record in self.records):
            raise KeyError(f"Unknown attempt: {attempt_id}")
        self._append(attempt_id, status, detail=detail, observed_state=observed_state)

    def latest(self, attempt_id: str | None = None) -> dict[str, Any] | None:
        records = self.records if attempt_id is None else [item for item in self.records if item.get("attempt_id") == attempt_id]
        return dict(records[-1]) if records else None


@dataclass(frozen=True)
class ProcessActivity:
    """Observed work below the root agent process."""

    active: bool = False
    descendants: tuple[int, ...] = ()
    direct_descendants: tuple[int, ...] = ()
    commands: tuple[str, ...] = ()
    cpu_percent: float = 0.0
    oldest_seconds: float = 0.0
    git_changed: bool = False
    duplicate_commands: tuple[str, ...] = ()
    expensive_roots: tuple[int, ...] = ()


@dataclass(frozen=True)
class LoopAssessment:
    """Fail-closed assessment for repeated or dangerous monitored-agent behavior."""

    detected: bool = False
    reason: str = ""
    evidence: tuple[str, ...] = ()
    occurrences: int = 0


EXPENSIVE_COMMAND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("full-test-suite", r"\b(?:npm\s+test|node\s+scripts/run-tests\.js|pytest(?:\s|$)|python\d*\s+-m\s+unittest)\b"),
    ("ci-watch", r"\bgh\s+(?:pr\s+checks|run\s+watch)\b"),
    ("build", r"\b(?:npm|pnpm|yarn)\s+run\s+build\b|\bcargo\s+build\b"),
)

GIT_HISTORY_REWRITE_PATTERNS: tuple[str, ...] = (
    r"\bgit\s+filter-branch\b",
    r"\bgit\s+rebase\b",
    r"\bgit\s+reset\s+--(?:soft|mixed|hard|keep|merge)\b",
    r"\bgit\s+commit\b[^\n]*(?:--amend|-c\s+HEAD|-C\s+HEAD)",
    r"\bgit\s+update-ref\s+refs/heads/",
)


def canonical_expensive_command(command: str) -> str:
    normalized = re.sub(r"\s+", " ", command).strip()
    for label, pattern in EXPENSIVE_COMMAND_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            return label
    return ""


def assess_agent_commands(
    commands: tuple[str, ...] | list[str],
    *,
    duplicate_commands: tuple[str, ...] | list[str] = (),
    allow_history_rewrite: bool = False,
) -> LoopAssessment:
    """Detect dangerous history rewrites and duplicate expensive command roots."""
    if duplicate_commands:
        return LoopAssessment(True, "duplicate_expensive_commands", tuple(duplicate_commands), len(duplicate_commands))
    if not allow_history_rewrite:
        for command in commands:
            if any(re.search(pattern, command, re.IGNORECASE) for pattern in GIT_HISTORY_REWRITE_PATTERNS):
                return LoopAssessment(True, "git_history_rewrite", (redact_sensitive(command),), 1)
    return LoopAssessment()


@dataclass
class AgentLoopGuard:
    """Track expensive command episodes and require observable progress between reruns."""

    repeat_limit: int = 3
    _progress_key: str = ""
    _episodes: list[str] = field(default_factory=list)
    _last_episode: str = ""

    def reset(self) -> None:
        self._episodes.clear()
        self._last_episode = ""

    def observe(
        self,
        snapshot_digest: str,
        progress_fingerprint: str,
        git_fingerprint: str,
        head: str,
        commands: tuple[str, ...] | list[str],
        *,
        episode: str = "",
    ) -> LoopAssessment:
        progress_key = "|".join((progress_fingerprint, git_fingerprint, head))
        if self._progress_key and progress_key != self._progress_key:
            self._episodes.clear()
            self._last_episode = ""
        self._progress_key = progress_key
        labels = sorted({label for command in commands if (label := canonical_expensive_command(command))})
        if not labels:
            self._last_episode = ""
            return LoopAssessment()
        episode_key = episode or f"{','.join(labels)}:{snapshot_digest}:{len(self._episodes)}"
        if episode and episode_key == self._last_episode:
            return LoopAssessment()
        self._last_episode = episode_key
        self._episodes.extend(labels)
        for label in labels:
            occurrences = self._episodes.count(label)
            if occurrences >= max(2, int(self.repeat_limit)):
                return LoopAssessment(True, "repeated_expensive_command_without_progress", (label,), occurrences)
        return LoopAssessment()


@dataclass
class SessionTracker:
    """Separates stale terminal history from output after the latest instruction."""

    interaction_history: str = ""
    generation: int = 0

    def mark_interaction(self, history: str) -> None:
        self.interaction_history = normalize_snapshot(history)
        self.generation += 1

    def current_segment(self, history: str) -> str:
        if not self.interaction_history:
            return history
        normalized = normalize_snapshot(history)
        if normalized == self.interaction_history:
            return ""
        position = normalized.rfind(self.interaction_history) if self.interaction_history else -1
        return normalized[position + len(self.interaction_history):].lstrip("\r\n") if position >= 0 else normalized

    def matches_current_completion(self, history: str, patterns: list[str] | tuple[str, ...]) -> bool:
        segment = self.current_segment(history)
        return bool(segment) and any(match_pattern(pattern, segment) for pattern in patterns)


@dataclass(frozen=True)
class PolicyEnvelope:
    """Permanent task policy wrapped around every dynamic instruction."""

    objective: str = ""
    prohibitions: tuple[str, ...] = ()

    def authorize_action(
        self,
        action: str,
        *,
        unsafe_phrases: list[str] | tuple[str, ...] = UNSAFE_PHRASES,
        npm_publish_allowed: bool = False,
    ) -> tuple[bool, str]:
        """Apply a durable, independent risk policy to an outbound action."""
        risk = classify_action_risk(action, npm_publish_allowed=npm_publish_allowed)
        if risk == "blocked":
            if _contains_positive_npm_publication(action) and not npm_publish_allowed:
                return False, "npm publication is prohibited by permanent policy"
            return False, "action blocked by permanent safety policy"
        if any(phrase.lower() in action.lower() for phrase in unsafe_phrases):
            return False, "action matches an unsafe phrase"
        if risk == "attention":
            return False, "high-risk action requires human attention"
        return True, "safe"

    def compose(self, dynamic: str, stage: str = "") -> str:
        low = dynamic.lower()
        npm_blocked = any("npm" in item.lower() and ("not" in item.lower() or "não" in item.lower()) for item in self.prohibitions)
        if npm_blocked and re.search(r"\b(publish|publique|publicar|publique)\b.{0,30}\bnpm\b|\bnpm\s+publish\b", low):
            raise ValueError("dynamic instruction conflicts with permanent npm prohibition")
        if classify_action_risk(dynamic) == "blocked":
            raise ValueError("dynamic instruction conflicts with permanent safety policy")
        parts = []
        if self.objective:
            parts.append(f"Objective: {self.objective}")
        if self.prohibitions:
            parts.append("Permanent prohibitions: " + " ".join(self.prohibitions))
        if stage:
            parts.append(f"Current stage: {stage}")
        if dynamic:
            parts.append(f"Next action: {dynamic}")
        return "\n".join(parts)


@dataclass(frozen=True)
class TerminalIdentity:
    """Hints used to choose the correct terminal among similar agent tabs."""

    project_path: str = ""
    branch: str = ""
    session_id: str = ""
    title: str = ""
    root_pid: int | None = None

    def score(self, candidate: dict[str, Any]) -> int:
        text = " ".join(str(candidate.get(key, "")) for key in ("history", "hist", "title", "wname"))
        score = 0
        if self.project_path and self.project_path in text:
            score += 8
        if self.session_id and self.session_id in text:
            score += 7
        if self.branch and self.branch in text:
            score += 5
        if self.title and self.title.lower() in str(candidate.get("title", "")).lower():
            score += 3
        if self.root_pid is not None and int(candidate.get("root_pid", -1)) == self.root_pid:
            score += 10
        return score


def interrupt_child(
    root_pids: set[int],
    child_pid: int,
    *,
    parent_of: Callable[[int], int | None],
    signaler: Callable[[int, int], Any] = os.kill,
) -> bool:
    """Interrupt a verified descendant while protecting every root agent PID."""
    if child_pid in root_pids or child_pid <= 1:
        return False
    seen: set[int] = set()
    current = child_pid
    while current not in seen and current > 1:
        seen.add(current)
        parent = parent_of(current)
        if parent is None:
            return False
        if parent in root_pids:
            signaler(child_pid, signal.SIGINT)
            return True
        current = parent
    return False


def interrupt_process_tree(
    root_pids: set[int],
    child_pid: int,
    *,
    parent_of: Callable[[int], int | None],
    children_of: Callable[[int], list[int]],
    signaler: Callable[[int, int], Any] = os.kill,
) -> bool:
    """Interrupt a verified descendant tree, deepest child first."""
    if child_pid in root_pids or child_pid <= 1:
        return False
    current = child_pid
    seen: set[int] = set()
    verified = False
    while current not in seen and current > 1:
        seen.add(current)
        parent = parent_of(current)
        if parent is None:
            break
        if parent in root_pids:
            verified = True
            break
        current = parent
    if not verified:
        return False

    ordered: list[int] = []
    visited: set[int] = set()

    def visit(pid: int) -> None:
        if pid in visited or pid in root_pids or pid <= 1:
            return
        visited.add(pid)
        for descendant in children_of(pid):
            visit(int(descendant))
        ordered.append(pid)

    visit(child_pid)
    for pid in ordered:
        with contextlib.suppress(ProcessLookupError):
            signaler(pid, signal.SIGINT)
    return True


def _children_pids(parent_pid: int) -> list[int]:
    code, output, _ = run_command(["ps", "-axo", "pid=,ppid="])
    if code != 0:
        return []
    children: list[int] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and all(part.isdigit() for part in parts) and int(parts[1]) == parent_pid:
            children.append(int(parts[0]))
    return children


def _elapsed_seconds(value: str) -> float:
    """Convert ps elapsed form ([[dd-]hh:]mm:)ss into seconds."""
    try:
        day_split = value.strip().split("-", 1)
        days = int(day_split[0]) if len(day_split) == 2 else 0
        clock = day_split[-1].split(":")
        numbers = [int(part) for part in clock]
        while len(numbers) < 3:
            numbers.insert(0, 0)
        hours, minutes, seconds = numbers[-3:]
        return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)
    except (ValueError, TypeError):
        return 0.0


def collect_process_activity(root_pids: list[int]) -> ProcessActivity:
    """Inspect descendants so long-running commands count as useful activity."""
    if not root_pids:
        return ProcessActivity()
    code, output, _ = run_command(["ps", "-axo", "pid=,ppid=,etime=,%cpu=,command="])
    if code != 0:
        return ProcessActivity()
    rows: dict[int, tuple[int, str, float, str]] = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        with contextlib.suppress(ValueError):
            rows[int(parts[0])] = (int(parts[1]), parts[2], float(parts[3]), parts[4])
    roots = set(root_pids)
    descendants: list[int] = []
    pending = set(roots)
    while pending:
        parent = pending.pop()
        children = [pid for pid, row in rows.items() if row[0] == parent and pid not in descendants]
        descendants.extend(children)
        pending.update(children)
    direct_descendants = tuple(pid for pid in descendants if pid in rows and rows[pid][0] in roots)
    commands = tuple(rows[pid][3] for pid in descendants if pid in rows)
    direct_labels = [canonical_expensive_command(rows[pid][3]) for pid in direct_descendants]
    duplicate_commands = tuple(sorted(label for label in set(direct_labels) if label and direct_labels.count(label) > 1))
    expensive_roots = tuple(pid for pid in direct_descendants if canonical_expensive_command(rows[pid][3]))
    cpu = sum(rows[pid][2] for pid in descendants if pid in rows)
    oldest = max((_elapsed_seconds(rows[pid][1]) for pid in descendants if pid in rows), default=0.0)
    meaningful = any(
        re.search(r"\b(pytest|unittest|npm\s+(?:test|run)|pnpm|yarn|cargo|go\s+test|git|gh|make|cmake|docker)\b", command, re.IGNORECASE)
        for command in commands
    )
    recently_started = any(_elapsed_seconds(rows[pid][1]) <= 5.0 for pid in descendants if pid in rows)
    active = bool(descendants) and (cpu >= 0.1 or meaningful or recently_started)
    return ProcessActivity(
        active=active,
        descendants=tuple(descendants),
        direct_descendants=direct_descendants,
        commands=commands,
        cpu_percent=cpu,
        oldest_seconds=oldest,
        duplicate_commands=duplicate_commands,
        expensive_roots=expensive_roots,
    )


RETRYABLE_CHECK_CONCLUSIONS = {"cancelled", "timed_out", "stale", "startup_failure", "action_required", "network_failure", "infrastructure_failure"}
CODE_FAILURE_CONCLUSIONS = {"failure"}
PASSED_CHECK_CONCLUSIONS = {"success", "neutral", "skipped"}
EXTERNAL_FAILURE_MARKERS = (
    "429",
    "408",
    "425",
    "502",
    "503",
    "504",
    "too many requests",
    "rate limit",
    "timed out",
    "timeout",
    "network",
    "connection reset",
    "temporary failure",
    "service unavailable",
)
HIGH_RISK_ACTION_MARKERS = (
    "npm publish",
    "npm unpublish",
    "npm version",
    "gh release create",
    "gh release delete",
    "git tag",
    "create release",
    "publish release",
)
NPM_PUBLICATION_PATTERNS = (
    re.compile(r"\bnpm\s+(?:publish|unpublish|version)\b", re.IGNORECASE),
    re.compile(r"\b(?:publish|unpublish|version)\s+(?:the\s+)?(?:package\s+)?to\s+npm\b", re.IGNORECASE),
)
NPM_NEGATION_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|never|not|no|não|nao|prohibit(?:ed)?|proibido|sem)(?:\s+\w+){0,2}\s*$",
    re.IGNORECASE,
)


def _contains_positive_npm_publication(action: str) -> bool:
    """Detect an npm publication command while allowing policy prohibitions themselves."""
    for pattern in NPM_PUBLICATION_PATTERNS:
        for match in pattern.finditer(action):
            prefix = action[max(0, match.start() - 48) : match.start()]
            if not NPM_NEGATION_PATTERN.search(prefix):
                return True
    return False


def classify_check_result(check: dict[str, Any]) -> dict[str, Any]:
    """Classify one GitHub check without conflating code and network failures."""
    raw = str(check.get("conclusion") or check.get("state") or check.get("status") or "").lower().strip()
    evidence = " ".join(
        str(check.get(key, ""))
        for key in ("name", "title", "summary", "output", "details", "message", "text")
    ).lower()
    if raw in PASSED_CHECK_CONCLUSIONS:
        category = "passed"
    elif raw in RETRYABLE_CHECK_CONCLUSIONS:
        category = "cancelled-infra"
    elif raw in CODE_FAILURE_CONCLUSIONS and any(marker in evidence for marker in EXTERNAL_FAILURE_MARKERS):
        category = "failed-external"
    elif raw in CODE_FAILURE_CONCLUSIONS:
        category = "failed"
    elif raw in {"queued", "in_progress", "pending", "requested", "waiting", ""}:
        category = "pending"
    else:
        category = "unknown"
    return {
        "category": category,
        "retryable": category in {"cancelled-infra", "failed-external"},
        "conclusion": raw,
        "evidence": evidence,
        "name": str(check.get("name") or check.get("context") or ""),
    }


def classify_action_risk(action: str, *, npm_publish_allowed: bool = False) -> str:
    """Return safe, attention, or blocked for a proposed external action."""
    low = action.lower()
    if not npm_publish_allowed and _contains_positive_npm_publication(action):
        return "blocked"
    for marker in HIGH_RISK_ACTION_MARKERS:
        if marker in low:
            if marker.startswith("npm ") and not _contains_positive_npm_publication(action):
                continue
            return "attention"
    if any(phrase in low for phrase in UNSAFE_PHRASES):
        return "blocked"
    return "safe"


class PullRequestStateMachine:
    """Map GitHub PR/check snapshots to an actionable supervision stage."""

    stage = "TASK_RECEIVED"
    seen_pr_number: int | None = None

    def advance(self, pr: dict[str, Any] | None) -> str:
        if not pr or not pr.get("number"):
            self.stage = "TASK_RECEIVED"
            return self.stage
        number = int(pr["number"])
        if self.seen_pr_number != number and self.stage == "TASK_RECEIVED":
            self.seen_pr_number = number
            self.stage = "PR_CREATED"
            return self.stage
        self.seen_pr_number = number
        checks = list(pr.get("checks") or [])
        classifications = [classify_check_result(check) for check in checks]
        conclusions = {item["category"] for item in classifications}
        if str(pr.get("state", "")).upper() == "MERGED":
            self.stage = "POST_MERGE_VERIFY"
        elif "failed" in conclusions:
            self.stage = "FIX_REQUIRED"
        elif conclusions & {"cancelled-infra", "failed-external"}:
            self.stage = "CI_RETRY_REQUIRED"
        elif checks and conclusions <= {"passed"}:
            self.stage = "CI_GREEN"
        else:
            self.stage = "CI_PENDING"
        return self.stage


@dataclass(frozen=True)
class FinalVerificationReport:
    ok: bool
    checks: dict[str, bool]
    failures: tuple[str, ...]


def evaluate_final_state(evidence: dict[str, Any]) -> FinalVerificationReport:
    """Evaluate post-merge evidence without weakening any required invariant."""
    checks = {
        "pr_merged": bool(evidence.get("pr_merged")),
        "checks_exact_head": bool(evidence.get("checks_green")) and bool(evidence.get("pr_head")) and evidence.get("pr_head") == evidence.get("checked_head"),
        "heads_synchronized": bool(evidence.get("local_head")) and evidence.get("local_head") == evidence.get("main_head") == evidence.get("origin_main_head"),
        "worktree_clean": bool(evidence.get("worktree_clean")),
        "npm_registry_unchanged": bool(evidence.get("npm_registry_unchanged")),
        "no_new_tag_or_release": bool(evidence.get("no_new_tag_or_release")),
        "no_publish_process": bool(evidence.get("no_publish_process")),
    }
    failures = tuple(name for name, passed in checks.items() if not passed)
    return FinalVerificationReport(ok=not failures, checks=checks, failures=failures)


def _command_value(command: list[str], cwd: str) -> str:
    code, output, _ = run_command(command, cwd=cwd)
    return output.strip() if code == 0 else ""


def git_activity_fingerprint(project_dir: str) -> str:
    """Cheap local-only fingerprint; deliberately avoids GitHub/network calls."""
    head = _command_value(["git", "rev-parse", "HEAD"], project_dir)
    status = _command_value(["git", "status", "--porcelain"], project_dir)
    return hashlib.sha256(f"{head}\n{status}".encode("utf-8", "replace")).hexdigest()


def collect_final_evidence(project_dir: str, state: TaskState, pr_number: int | None = None) -> dict[str, Any]:
    """Collect live evidence used by `verify-final-state`."""
    local_head = _command_value(["git", "rev-parse", "HEAD"], project_dir)
    main_head = _command_value(["git", "rev-parse", "main"], project_dir)
    origin_main_head = _command_value(["git", "rev-parse", "origin/main"], project_dir)
    status = _command_value(["git", "status", "--porcelain"], project_dir)
    pr_ref = str(pr_number or state.pr.get("number") or state.branch or "")
    pr_data: dict[str, Any] = {}
    if pr_ref and shutil.which("gh"):
        code, output, _ = run_command(
            ["gh", "pr", "view", pr_ref, "--json", "state,headRefOid,statusCheckRollup"],
            cwd=project_dir,
        )
        if code == 0:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                pr_data = json.loads(output)
    rollup = list(pr_data.get("statusCheckRollup") or [])
    conclusions = [str(item.get("conclusion") or item.get("state") or "").lower() for item in rollup]
    checks_green = bool(rollup) and all(item in {"success", "neutral", "skipped"} for item in conclusions)
    head = str(pr_data.get("headRefOid") or state.pr.get("head") or "")
    tags_before = set(state.pr.get("tagsBefore") or [])
    releases_before = set(state.pr.get("releasesBefore") or [])
    tags_now = set(filter(None, _command_value(["git", "tag", "--list"], project_dir).splitlines()))
    releases_now: set[str] = set()
    if shutil.which("gh"):
        code, output, _ = run_command(["gh", "release", "list", "--limit", "100", "--json", "tagName"], cwd=project_dir)
        if code == 0:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                releases_now = {str(item["tagName"]) for item in json.loads(output)}
    publish_code, publish_output, _ = run_command(["pgrep", "-af", r"(?:^|/)(?:npm|pnpm|yarn)(?:\s|$)"])
    publish_processes = []
    for line in publish_output.splitlines():
        parts = line.split(None, 1)
        command = parts[1] if len(parts) == 2 else line
        executable = command.split(None, 1)[0] if command else ""
        if Path(executable).name in {"npm", "pnpm", "yarn"} and re.search(r"\bpublish\b", command):
            publish_processes.append(line)
    package_json = Path(project_dir, "package.json")
    npm_unchanged = True
    expected_npm = state.pr.get("npmVersionBefore")
    if package_json.is_file() and expected_npm is not None:
        with contextlib.suppress(OSError, json.JSONDecodeError, KeyError):
            package_name = json.loads(package_json.read_text(encoding="utf-8"))["name"]
            current_npm = _command_value(["npm", "view", str(package_name), "version"], project_dir)
            npm_unchanged = current_npm == str(expected_npm)
    baseline_known = bool(state.pr.get("safetyBaselineCaptured"))
    return {
        "pr_merged": str(pr_data.get("state", "")).upper() == "MERGED",
        "pr_head": head,
        "checked_head": head if rollup else "",
        "checks_green": checks_green,
        "local_head": local_head,
        "main_head": main_head,
        "origin_main_head": origin_main_head,
        "worktree_clean": not status,
        "npm_registry_unchanged": npm_unchanged,
        "no_new_tag_or_release": baseline_known and tags_now == tags_before and releases_now == releases_before,
        "no_publish_process": publish_code != 0 or not publish_processes,
    }


def capture_safety_baseline(project_dir: str) -> dict[str, Any]:
    """Capture tag, release, and npm state before autonomous work advances."""
    tags = list(filter(None, _command_value(["git", "tag", "--list"], project_dir).splitlines()))
    releases: list[str] = []
    if shutil.which("gh"):
        code, output, _ = run_command(["gh", "release", "list", "--limit", "100", "--json", "tagName"], cwd=project_dir)
        if code == 0:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                releases = [str(item["tagName"]) for item in json.loads(output)]
    baseline: dict[str, Any] = {
        "safetyBaselineCaptured": True,
        "tagsBefore": tags,
        "releasesBefore": releases,
    }
    package_json = Path(project_dir, "package.json")
    if package_json.is_file() and shutil.which("npm"):
        with contextlib.suppress(OSError, json.JSONDecodeError, KeyError):
            package = json.loads(package_json.read_text(encoding="utf-8"))
            if not package.get("private"):
                version = _command_value(["npm", "view", str(package["name"]), "version"], project_dir)
                baseline["npmVersionBefore"] = version
    return baseline


def _parent_pid(pid: int) -> int | None:
    value = _command_value(["ps", "-o", "ppid=", "-p", str(pid)], ".")
    return int(value) if value.isdigit() else None


def build_restart_command(config: MonitorConfig, state: TaskState, explicit: list[str] | None, continue_session: bool) -> list[str]:
    """Build a restart command without invoking a shell."""
    command = list(explicit or [config.process])
    if continue_session:
        if not state.session_id:
            raise StateFileError("No saved sessionId is available for continuation")
        if config.profile == "opencode" or config.process == "opencode":
            command.extend(["--session", state.session_id])
        else:
            command.extend(["--continue", state.session_id])
    return command


def get_current_pr_snapshot(project_dir: str, reference: str = "") -> dict[str, Any] | None:
    """Return normalized PR/check data for the saved PR or current branch."""
    if not shutil.which("gh"):
        return None
    command = ["gh", "pr", "view"]
    if reference:
        command.append(reference)
    command.extend(["--json", "number,state,headRefOid,statusCheckRollup"])
    code, output, _ = run_command(command, cwd=project_dir)
    if code != 0:
        return None
    try:
        data = json.loads(output)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def evaluate_repository_safety(
    status: GitStatus,
    *,
    expected_branch: str = "",
    protected_branches: tuple[str, ...] = ("main", "master"),
) -> dict[str, Any]:
    """Fail closed when supervision observes an unexpected or dirty branch."""
    if not status.is_repo:
        return {
            "safe": False,
            "reason": "not_a_repository",
            "branch": status.branch,
            "head": status.head,
            "dirty": status.dirty,
            "modified_files": list(status.modified_files),
        }
    if status.branch in protected_branches and status.dirty:
        return {
            "safe": False,
            "reason": "protected_branch_dirty",
            "branch": status.branch,
            "head": status.head,
            "dirty": True,
            "modified_files": list(status.modified_files),
        }
    if expected_branch and status.branch != expected_branch:
        return {
            "safe": False,
            "reason": "branch_mismatch",
            "branch": status.branch,
            "head": status.head,
            "expected_branch": expected_branch,
            "dirty": status.dirty,
            "modified_files": list(status.modified_files),
        }
    return {
        "safe": True,
        "reason": "ok",
        "branch": status.branch,
        "head": status.head,
        "expected_branch": expected_branch,
        "dirty": status.dirty,
        "modified_files": list(status.modified_files),
    }


def verify_merge_gate(project_dir: str, pr_number: int, expected_head: str) -> dict[str, Any]:
    """Re-query the PR and require green checks for the exact expected head."""
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head or "", flags=re.IGNORECASE):
        return {
            "ok": False,
            "reason": "invalid_expected_head",
            "expected_head": expected_head,
        }
    code, output, error = run_command(
        ["gh", "pr", "view", str(pr_number), "--json", "number,state,headRefOid,statusCheckRollup"],
        cwd=project_dir,
    )
    if code != 0:
        return {"ok": False, "reason": "github_query_failed", "detail": error or output}
    try:
        pr = json.loads(output)
    except (json.JSONDecodeError, TypeError) as exc:
        return {"ok": False, "reason": "invalid_github_response", "detail": str(exc)}
    state = str(pr.get("state", "")).upper()
    if state == "MERGED":
        return {"ok": True, "reason": "already_merged", "pr": pr, "head": pr.get("headRefOid", ""), "checks": []}
    if state != "OPEN":
        return {"ok": False, "reason": "pr_not_open", "state": state, "pr": pr}
    actual_head = str(pr.get("headRefOid", ""))
    if not expected_head or actual_head != expected_head:
        return {"ok": False, "reason": "head_mismatch", "expected_head": expected_head, "actual_head": actual_head, "pr": pr}
    checks = [classify_check_result(check) for check in pr.get("statusCheckRollup") or []]
    if not checks:
        return {"ok": False, "reason": "checks_missing", "head": actual_head, "checks": checks, "pr": pr}
    bad = [item for item in checks if item["category"] != "passed"]
    if bad:
        reason = "checks_retryable" if any(item["retryable"] for item in bad) else "checks_not_green"
        return {"ok": False, "reason": reason, "head": actual_head, "checks": checks, "pr": pr}
    return {"ok": True, "reason": "green_exact_head", "head": actual_head, "checks": checks, "pr": pr}


def merge_pull_request(project_dir: str, pr_number: int, expected_head: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Merge only after the exact-head gate; dry-run never contacts GitHub."""
    if dry_run:
        return {"ok": True, "dry_run": True, "reason": "would_merge", "pr": pr_number, "head": expected_head}
    gate = verify_merge_gate(project_dir, pr_number, expected_head)
    if not gate.get("ok"):
        return {**gate, "merged": False}
    if gate.get("reason") == "already_merged":
        return {**gate, "merged": True}
    code, output, error = run_command(
        ["gh", "pr", "merge", str(pr_number), "--merge", "--match-head-commit", expected_head],
        cwd=project_dir,
    )
    if code != 0:
        return {"ok": False, "merged": False, "reason": "merge_failed", "detail": error or output, "gate": gate}
    return {"ok": True, "merged": True, "reason": "merged", "detail": output, "gate": gate}


def persist_restart_event(state_path: str | Path, state: TaskState, command: list[str]) -> TaskState:
    """Persist enough restart context to resume without replaying an old prompt."""
    metadata = dict(state.pr)
    metadata["lastRestart"] = {
        "timestamp": now_iso(),
        "sessionId": state.session_id,
        "command": list(command),
    }
    updated = replace(state, pr=metadata)
    updated.save(state_path)
    return updated


def write_final_report(
    path: str | Path,
    state: TaskState,
    evidence: dict[str, Any],
    report: FinalVerificationReport,
) -> None:
    """Write an auditable final report with policy and continuation history."""
    payload = {
        "generated_at": now_iso(),
        "ok": report.ok,
        "checks": report.checks,
        "failures": list(report.failures),
        "evidence": evidence,
        "attempts": list(state.attempts),
        "ci_events": list(state.ci_events),
        "policy_decisions": list(state.policy_decisions),
        "prohibitions": list(state.prohibitions),
        "npm_publish_allowed": state.npm_publish_allowed,
        "npm_publication_prohibited": not state.npm_publish_allowed,
    }
    _atomic_json_write(path, payload)


def retry_infrastructure_checks(project_dir: str, pr: dict[str, Any]) -> list[int]:
    """Retry only workflow runs whose check outcome is infrastructure-like."""
    retried: list[int] = []
    for check in pr.get("statusCheckRollup") or pr.get("checks") or []:
        classification = classify_check_result(check)
        if not classification["retryable"]:
            continue
        url = str(check.get("detailsUrl") or check.get("link") or "")
        match = re.search(r"/actions/runs/(\d+)", url)
        if not match:
            continue
        run_id = int(match.group(1))
        code, _, _ = run_command(["gh", "run", "rerun", str(run_id), "--failed"], cwd=project_dir)
        if code == 0:
            retried.append(run_id)
    return retried


def wait_for_change(
    fingerprint: Callable[[], str],
    initial: str,
    *,
    timeout_seconds: float,
    interval_seconds: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until observable state changes, bounded by a portability timeout."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if fingerprint() != initial:
            return True
        sleeper(min(interval_seconds, max(0.0, deadline - time.monotonic())))
    return False


def wait_for_ci_event(project_dir: str, pr_number: int, *, timeout_seconds: float = 30.0) -> bool:
    """Use GitHub's watch stream for CI changes instead of status polling."""
    command = ["gh", "pr", "checks", str(pr_number), "--watch", "--interval", "5"]
    try:
        subprocess.run(
            command,
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1.0, timeout_seconds),
        )
        return True
    except (subprocess.TimeoutExpired, OSError):
        return False


def match_pattern(pattern: str, text: str) -> bool:
    """Match a pattern against text safely, supporting regex or substring."""
    low_text = text.lower()
    low_pat = pattern.lower()
    if low_pat in low_text:
        return True
    if any(token in pattern for token in (r"\b", r"\s", r"\d", r"\S", r"\w", "^", "$", "|", ".*", ".+", "?:")):
        try:
            return bool(re.search(pattern, text, re.IGNORECASE | re.MULTILINE))
        except re.error:
            pass
    return False


def is_table_or_box_line(line: str) -> bool:
    """Check if a line is part of a markdown or unicode box/table."""
    stripped = line.strip()
    if not stripped:
        return False
    if re.search(r"[┌├└┬┴┼─━═]", stripped):
        return True
    if stripped.count("│") >= 2 or stripped.count("|") >= 2:
        return True
    return bool(re.match(r"^[\s|+_=-]+$", stripped))


# ---------------------------------------------------------------------------
# Agent Profiles
# ---------------------------------------------------------------------------

@dataclass
class AgentProfile:
    """Configuration profile describing how to detect and interact with an AI agent CLI."""

    name: str
    process: str = "generic"
    description: str = ""
    thinking_patterns: list[str] = field(default_factory=list)
    permission_patterns: list[str] = field(default_factory=list)
    question_indicators: list[str] = field(default_factory=list)
    option_patterns: list[str] = field(default_factory=list)
    unsafe_phrases: list[str] = field(default_factory=lambda: list(UNSAFE_PHRASES))
    preferred_answers: list[str] = field(default_factory=lambda: list(DEFAULT_PREFERRED_ANSWERS))
    auto_permission_payload: str = ""
    default_continue_text: str | None = None
    mode_patterns: dict[str, str] = field(default_factory=dict)
    plan_ready_patterns: list[str] = field(default_factory=list)
    mode_switch_key: str = "tab"
    completion_patterns: list[str] = field(default_factory=list)

    def matches_thinking(self, history_tail: str) -> bool:
        return any(match_pattern(pat, history_tail) for pat in self.thinking_patterns)

    def matches_permission(self, history_tail: str) -> bool:
        return any(match_pattern(pat, history_tail) for pat in self.permission_patterns)

    def matches_question(self, history_tail: str) -> bool:
        options = self.extract_options(history_tail)
        if len(options) < 2:
            return False
        strong_prompt = any(match_pattern(pat, history_tail) for pat in self.question_indicators)
        strong_prompt = strong_prompt or bool(
            re.search(
                r"(?:\?|\b(?:which|choose|select|pick|qual|escolha|selecione)\b|\[y/n\]|⇆\s*select)",
                history_tail,
                re.IGNORECASE,
            )
        )
        return strong_prompt

    def matches_completion(self, history_tail: str) -> bool:
        if not self.completion_patterns:
            return False
        return any(match_pattern(pat, history_tail) for pat in self.completion_patterns)

    def detect_mode(self, history_tail: str) -> str | None:
        """Detect the active TUI mode from history tail if patterns are configured."""
        if not self.mode_patterns:
            return None
        for mode, pattern in self.mode_patterns.items():
            if re.search(pattern, history_tail, re.IGNORECASE | re.MULTILINE):
                return mode
        return None

    def is_plan_ready(self, history_tail: str) -> bool:
        """Detect if the agent has finished planning and is waiting for approval."""
        if not self.plan_ready_patterns:
            return False
        return any(match_pattern(pat, history_tail) for pat in self.plan_ready_patterns)

    def extract_options(self, history_tail: str) -> list[tuple[str, bool]]:
        """Extract selectable options and their recommendation status from history tail."""
        options: list[tuple[str, bool]] = []
        in_code_block = False
        for line in history_tail.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue
            if is_table_or_box_line(line):
                continue

            is_option = any(match_pattern(pat, line) for pat in self.option_patterns)
            if not is_option and re.search(r"recommended", line, re.IGNORECASE):
                is_option = True

            if is_option:
                clean = clean_option(line)
                if clean and len(clean) >= 2:
                    is_rec = bool(re.search(r"recommended", line, re.IGNORECASE))
                    options.append((clean, is_rec))
        return options


BUILTIN_PROFILES: dict[str, AgentProfile] = {
    "opencode": AgentProfile(
        name="opencode",
        process="opencode",
        description="Profile for OpenCode agent CLI",
        thinking_patterns=[
            "esc interrupt",
            "preparing write",
            "thinking...",
            "working...",
            r"~ (writing|updating|reading|running)",
        ],
        permission_patterns=[
            r"allow.*deny",
            "allow once",
            "allow always",
            "permission required",
            "do you want to run",
        ],
        question_indicators=[
            "(recommended)",
            r"\b(choose|select|which option|pick one|what should)\b",
            "⇆ tab",
            "⇆ select",
            "enter confirm",
        ],
        option_patterns=[
            r"^\s*[●○◉❯]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="",
        mode_patterns={
            "plan": r"Plan\s*·\s*\w+",
            "build": r"Build\s*·\s*\w+",
        },
        plan_ready_patterns=[
            "plano pronto",
            "plan ready",
            "plan complete",
            "aprove para eu sair do modo plano",
            "ready to build",
        ],
        mode_switch_key="tab",
        completion_patterns=[
            "100% concluído",
            "todas as tarefas estão concluídas",
            "todas as 20 tasks estão concluídas",
            "não há próxima task",
            "não há trabalho restante no plano",
            "all tasks completed",
            "plan is complete",
            "todos os prs mergeados",
        ],
    ),
    "claude": AgentProfile(
        name="claude",
        process="claude",
        description="Profile for Anthropic Claude Code CLI",
        thinking_patterns=[
            "thinking...",
            "thinking",
            "esc interrupt",
            "esc to cancel",
            "running tool",
            "reading file",
            "writing file",
            "running command",
            "waiting for response",
            "fetching...",
        ],
        permission_patterns=[
            "allow once",
            "allow this tool",
            "allow always",
            "do you want to run",
            "[y/n]",
            "yes / no",
            "approve tool",
            "press enter to continue",
        ],
        question_indicators=[
            "(recommended)",
            r"\b(select|choose|which option|pick)\b",
            r"\b(approve|deny|reject)\b",
            r"\[yes\]:",
            r"\(y\)es/\(n\)o",
            r"\bquestion\b",
        ],
        option_patterns=[
            r"^\s*[●○◉❯>]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="y",
        completion_patterns=[
            "all tasks complete",
            "task completed successfully",
            "done with all tasks",
        ],
    ),
    "claude-code": AgentProfile(
        name="claude-code",
        process="claude",
        description="Alias for Anthropic Claude Code CLI (derived from the claude profile)",
    ),
    "aider": AgentProfile(
        name="aider",
        process="aider",
        description="Profile for Aider pair programming CLI",
        thinking_patterns=[
            "thinking...",
            "analyzing",
            "generating code",
            "processing",
            "updating repo",
            "indexing",
            "searching",
        ],
        permission_patterns=[
            "run command?",
            "(y)es/(n)o",
            "apply changes?",
            "add them to the chat?",
            "create a new file?",
            "run the test command?",
        ],
        question_indicators=[
            r"\(y\)es/\(n\)o",
            r"\[yes\]:",
            "(recommended)",
        ],
        option_patterns=[
            r"^\s*\d+[.)\]]\s+\S",
            r"^\s*[●○◉]\s+\S",
        ],
        auto_permission_payload="y",
    ),
    "goose": AgentProfile(
        name="goose",
        process="goose",
        description="Profile for Block Goose AI agent CLI",
        thinking_patterns=[
            "thinking...",
            "working...",
            "calling tool...",
            "executing...",
        ],
        permission_patterns=[
            "permission required",
            "approve",
            "deny",
            "allow this action",
        ],
        question_indicators=[
            "(recommended)",
            r"\b(choose|select|pick)\b",
        ],
        option_patterns=[
            r"^\s*[●○◉]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="y",
    ),
    "generic": AgentProfile(
        name="generic",
        process="agent",
        description="Generic fallback profile for any AI CLI agent",
        thinking_patterns=[
            "thinking...",
            "working...",
            "processing...",
            "generating...",
            "please wait...",
            "esc to cancel",
            "esc interrupt",
        ],
        permission_patterns=[
            r"allow.*deny",
            "allow once",
            "permission required",
            "permission",
            "approve",
            "[y/n]",
            "allow this action",
        ],
        question_indicators=[
            "(recommended)",
            r"^\s*[●○◉]\s+\S",
            r"\b(choose|select|which option|pick one|what should)\b",
        ],
        option_patterns=[
            r"^\s*[●○◉]\s+\S",
            r"^\s*\d+[.)\]]\s+\S",
        ],
        auto_permission_payload="",
    ),
}


# claude-code is a full alias of the claude profile, derived to avoid duplication.
BUILTIN_PROFILES["claude-code"] = replace(
    BUILTIN_PROFILES["claude"],
    name="claude-code",
    description="Alias for Anthropic Claude Code CLI",
)


def get_profile(name_or_process: str | None = None, custom_profiles: dict[str, Any] | None = None) -> AgentProfile:
    """Resolve an AgentProfile by name, process, or dictionary config."""
    custom = custom_profiles or {}
    key = (name_or_process or "generic").lower().strip()

    # Match in custom profiles first
    if key in custom:
        val = custom[key]
        if isinstance(val, AgentProfile):
            return val
        if isinstance(val, dict):
            return AgentProfile(
                name=key,
                process=val.get("process", key),
                description=val.get("description", ""),
                thinking_patterns=val.get("thinking_patterns", []),
                permission_patterns=val.get("permission_patterns", []),
                question_indicators=val.get("question_indicators", []),
                option_patterns=val.get("option_patterns", [r"^\s*[●○◉]\s+\S", r"^\s*\d+[.)\]]\s+\S"]),
                unsafe_phrases=val.get("unsafe_phrases", list(UNSAFE_PHRASES)),
                preferred_answers=val.get("preferred_answers", list(DEFAULT_PREFERRED_ANSWERS)),
                auto_permission_payload=val.get("auto_permission_payload", ""),
                default_continue_text=val.get("default_continue_text"),
                mode_patterns=val.get("mode_patterns", {}),
                plan_ready_patterns=val.get("plan_ready_patterns", []),
                mode_switch_key=val.get("mode_switch_key", "tab"),
                completion_patterns=val.get("completion_patterns", []),
            )

    # Match in built-in profiles
    if key in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[key]

    # Try matching process name against built-in profiles
    for prof in BUILTIN_PROFILES.values():
        if prof.process.lower() == key:
            return prof

    # Fallback generic profile with the requested process name
    generic = BUILTIN_PROFILES["generic"]
    return AgentProfile(
        name=key,
        process=key,
        description=f"Auto-generated profile for {key}",
        thinking_patterns=list(generic.thinking_patterns),
        permission_patterns=list(generic.permission_patterns),
        question_indicators=list(generic.question_indicators),
        option_patterns=list(generic.option_patterns),
        unsafe_phrases=list(generic.unsafe_phrases),
        preferred_answers=list(generic.preferred_answers),
        auto_permission_payload=generic.auto_permission_payload,
        mode_patterns=dict(generic.mode_patterns),
        plan_ready_patterns=list(generic.plan_ready_patterns),
        mode_switch_key=generic.mode_switch_key,
        completion_patterns=list(generic.completion_patterns),
    )


def list_profiles(custom_profiles: dict[str, Any] | None = None) -> dict[str, str]:
    """Return dictionary of all available profiles and their descriptions."""
    profiles: dict[str, str] = {}
    for name, prof in BUILTIN_PROFILES.items():
        profiles[name] = prof.description or f"Profile for {name}"
    if custom_profiles:
        for name, data in custom_profiles.items():
            desc = data.get("description", "Custom profile") if isinstance(data, dict) else getattr(data, "description", "Custom profile")
            profiles[name] = desc
    return profiles


# ---------------------------------------------------------------------------
# Configuration Class
# ---------------------------------------------------------------------------

@dataclass
class MonitorConfig:
    """Comprehensive and modular monitor configuration."""

    process: str = "opencode"
    profile: str = "opencode"
    title: str | None = None
    continue_text: str = ""
    continue_file: str | None = None
    poll_seconds: float = 3.0
    idle_seconds: float = 15.0
    cooldown_seconds: float = 20.0
    gone_seconds: float = 25.0
    max_sends: int = 100
    auto_allow_permissions: bool = False
    once: bool = False
    dry_run: bool = False
    state_dir: str = "/tmp/terminal-monitor"
    backend: str = "auto"
    project_dir: str = "."
    unsafe_phrases: list[str] = field(default_factory=lambda: list(UNSAFE_PHRASES))
    custom_profiles: dict[str, Any] = field(default_factory=dict)
    # Supervision & Autonomous Extensions
    supervise: bool = False
    auto_switch_modes: bool = True
    smart_nudges: bool = True
    completion_check: bool = True
    status_json_path: str | None = None
    objective: str = ""
    prohibitions: list[str] = field(default_factory=list)
    task_id: str = ""
    required_outcome: str = "merged"
    npm_publish_allowed: bool = False
    session_id: str = ""
    expected_branch: str = ""
    protected_branches: tuple[str, ...] = ("main", "master")
    report_path: str | None = None
    attempt_history_limit: int = 100
    loop_guard: bool = True
    loop_repeat_limit: int = 3
    queued_attempt_seconds: float = 45.0
    allow_history_rewrite: bool = False
    web_ui: bool = True
    web_port: int = 8765
    web_open_browser: bool = True
    launch_command: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Terminal Backends
# ---------------------------------------------------------------------------

def validate_process_name(process: str) -> str:
    """Ensure process name contains only safe alphanumeric/dash/underscore characters."""
    clean = process.strip()
    if not clean or not re.match(r"^[A-Za-z0-9_.-]+$", clean):
        raise ValueError(f"Invalid process name: {process!r}")
    return clean


def validate_title_filter(title: str | None) -> str | None:
    """Normalize and sanity-check a window title substring filter.

    Rejects newlines/control characters and caps length so the value stays a
    safe single-line AppleScript string literal (escaping still applied later).
    """
    if title is None:
        return None
    clean = title.strip()
    if not clean:
        return None
    if len(clean) > 200:
        raise ValueError("Title filter too long (max 200 characters)")
    if re.search(r"[\x00-\x1f\x7f]", clean):
        raise ValueError(f"Title filter contains control characters: {title!r}")
    return clean


def applescript_escape(value: str) -> str:
    """Escape backslashes and double quotes for AppleScript literal strings."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def applescript_terminal_title_condition(title: str | None) -> str:
    """Prefer a tab's custom title before falling back to its window name.

    Terminal.app appends the application name to every window name.  A broad
    filter such as ``OpenCode`` therefore matched an unrelated OpenCode tab
    whose custom title was ``OC | ...``.  Only tabs without a custom title may
    use the window-name fallback.
    """
    checked_title = validate_title_filter(title)
    if not checked_title:
        return "set titleOK to true"
    wanted = applescript_escape(checked_title)
    return f'set titleOK to ((ttitle contains "{wanted}") or (ttitle is "" and wname contains "{wanted}"))'


OSASCRIPT_TIMEOUT_SECONDS = 15.0
COMMAND_TIMEOUT_SECONDS = 30.0


def run_osascript_timeout_message() -> str:
    """Return the standardized error message emitted when osascript times out."""
    return f"osascript timed out after {int(OSASCRIPT_TIMEOUT_SECONDS)}s"


def run_osascript(script: str) -> tuple[int, str]:
    """Execute AppleScript via osascript subprocess safely with a hard timeout."""
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=OSASCRIPT_TIMEOUT_SECONDS,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return proc.returncode, out or err
    except subprocess.TimeoutExpired:
        return 1, run_osascript_timeout_message()
    except Exception as exc:
        return 1, str(exc)


def run_command(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a local command and return returncode, stdout, stderr with a hard timeout."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"command timed out after {int(COMMAND_TIMEOUT_SECONDS)}s"
    except Exception as exc:
        return 1, "", str(exc)


def parse_tab_output(raw: str) -> dict[str, str | bool]:
    """Parse key-value output emitted by AppleScript tab queries."""
    if raw.strip() == "MISSING":
        return {"ok": False, "error": "matching Terminal tab not found"}

    data: dict[str, str | bool] = {"ok": True, "error": ""}
    lines = raw.splitlines()
    hist_lines: list[str] = []
    in_hist = False

    for line in lines:
        if in_hist:
            hist_lines.append(line)
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            if key == "WIN":
                data["win"] = val
            elif key == "TAB":
                data["tab"] = val
            elif key == "TITLE":
                data["title"] = val
            elif key == "BUSY":
                data["busy"] = val.lower() == "true"
            elif key == "WNAME":
                data["wname"] = val
            elif key == "HIST":
                in_hist = True
                hist_lines.append(val)

    data["hist"] = "\n".join(hist_lines)
    return data


class BaseTerminalBackend:
    """Abstract base class for terminal interaction backends."""

    def name(self) -> str:
        raise NotImplementedError

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        raise NotImplementedError

    def get_tab_for_identity(self, process: str, identity: TerminalIdentity) -> dict[str, str | bool]:
        """Resolve a tab using progressively broader stable identity hints."""
        hints = [
            identity.title,
            identity.session_id,
            identity.branch,
            Path(identity.project_path).name if identity.project_path else "",
        ]
        seen: set[str | None] = set()
        for hint in [*hints, None]:
            normalized = hint or None
            if normalized in seen:
                continue
            seen.add(normalized)
            tab = self.get_tab(process, normalized)
            if tab.get("ok"):
                return tab
        return {"ok": False, "error": "matching terminal identity not found"}

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        raise NotImplementedError

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        """Send a special key or control sequence to the target tab."""
        return self.send(process, title, key)

    def get_pids(self, process: str) -> list[int]:
        """Return list of active process IDs matching process name."""
        process = validate_process_name(process)
        if not shutil.which("pgrep"):
            return []
        code, out, _ = run_command(["pgrep", "-x", process])
        if code != 0 or not out:
            return []
        pids: list[int] = []
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids


class TerminalAppBackend(BaseTerminalBackend):
    """Native macOS Terminal.app backend via AppleScript."""

    def name(self) -> str:
        return "terminal"

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        process = validate_process_name(process)
        process_literal = applescript_escape(process)
        title_check = applescript_terminal_title_condition(title)
        script = f'''
tell application "Terminal"
  repeat with w from 1 to count of windows
    repeat with t from 1 to count of tabs of window w
      if ((processes of tab t of window w) as string) contains "{process_literal}" then
        set ttitle to ""
        try
          set ttitle to (custom title of tab t of window w) as string
        end try
        set wname to (name of window w) as string
        {title_check}
        if titleOK then
          return "WIN=" & w & linefeed & "TAB=" & t & linefeed & "TITLE=" & ttitle & linefeed & "BUSY=" & ((busy of tab t of window w) as string) & linefeed & "WNAME=" & wname & linefeed & "HIST=" & ((history of tab t of window w) as string)
        end if
      end if
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        if code:
            return {"ok": False, "error": output}
        return parse_tab_output(output)

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        process_literal = applescript_escape(validate_process_name(process))
        title_check = applescript_terminal_title_condition(title)
        escaped_payload = applescript_escape(re.sub(r"\s+", " ", payload).strip())
        script = f'''
tell application "Terminal"
  repeat with w from 1 to count of windows
    repeat with t from 1 to count of tabs of window w
      if ((processes of tab t of window w) as string) contains "{process_literal}" then
        set ttitle to ""
        try
          set ttitle to (custom title of tab t of window w) as string
        end try
        set wname to (name of window w) as string
        {title_check}
        if titleOK then
          do script "{escaped_payload}" in tab t of window w
          return "SENT"
        end if
      end if
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        return code == 0 and output == "SENT", output

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        """Send a special key code directly via native AppleScript without System Events."""
        process_literal = applescript_escape(validate_process_name(process))
        key_normalized = key.lower().strip()
        char_id = SPECIAL_KEY_CODES.get(key_normalized)
        if char_id is None:
            if len(key) == 1:
                char_id = ord(key)
            else:
                return self.send(process, title, key)

        title_check = applescript_terminal_title_condition(title)
        script = f'''
tell application "Terminal"
  repeat with w from 1 to count of windows
    repeat with t from 1 to count of tabs of window w
      if ((processes of tab t of window w) as string) contains "{process_literal}" then
        set ttitle to ""
        try
          set ttitle to (custom title of tab t of window w) as string
        end try
        set wname to (name of window w) as string
        {title_check}
        if titleOK then
          do script (character id {char_id}) in tab t of window w
          return "SENT"
        end if
      end if
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        return code == 0 and output == "SENT", output


class ITerm2Backend(BaseTerminalBackend):
    """Native macOS iTerm2 backend via AppleScript."""

    def name(self) -> str:
        return "iterm2"

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        process_literal = applescript_escape(validate_process_name(process))
        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
            title_check = f'set titleOK to (sname contains "{wanted}")'
        script = f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (variable named "commandLine" of s as string) contains "{process_literal}" or (name of s as string) contains "{process_literal}" then
          set sname to (name of s) as string
          {title_check}
          if titleOK then
            return "WIN=" & (id of w) & linefeed & "TAB=" & (id of t) & linefeed & "TITLE=" & sname & linefeed & "BUSY=false" & linefeed & "WNAME=" & sname & linefeed & "HIST=" & (text of s as string)
          end if
        end if
      end repeat
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        if code:
            return {"ok": False, "error": output}
        return parse_tab_output(output)

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        process_literal = applescript_escape(validate_process_name(process))
        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
            title_check = f'set titleOK to (sname contains "{wanted}")'
        escaped_payload = applescript_escape(re.sub(r"\s+", " ", payload).strip())
        script = f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (variable named "commandLine" of s as string) contains "{process_literal}" or (name of s as string) contains "{process_literal}" then
          set sname to (name of s) as string
          {title_check}
          if titleOK then
            tell s to write text "{escaped_payload}"
            return "SENT"
          end if
        end if
      end repeat
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        return code == 0 and output == "SENT", output

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        """Send a special key code directly via iTerm2 write text without newline."""
        process_literal = applescript_escape(validate_process_name(process))
        key_normalized = key.lower().strip()
        char_id = SPECIAL_KEY_CODES.get(key_normalized)
        if char_id is None:
            if len(key) == 1:
                char_id = ord(key)
            else:
                return self.send(process, title, key)

        checked_title = validate_title_filter(title)
        title_check = "set titleOK to true"
        if checked_title:
            wanted = applescript_escape(checked_title)
            title_check = f'set titleOK to (sname contains "{wanted}")'
        script = f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (variable named "commandLine" of s as string) contains "{process_literal}" or (name of s as string) contains "{process_literal}" then
          set sname to (name of s) as string
          {title_check}
          if titleOK then
            tell s to write text (character id {char_id}) without newline
            return "SENT"
          end if
        end if
      end repeat
    end repeat
  end repeat
  return "MISSING"
end tell
'''
        code, output = run_osascript(script)
        return code == 0 and output == "SENT", output


class TmuxBackend(BaseTerminalBackend):
    """Cross-platform terminal backend using tmux capture-pane and send-keys."""

    def name(self) -> str:
        return "tmux"

    def _find_target(self, process: str, title: str | None = None) -> str | None:
        if not shutil.which("tmux"):
            return None
        title = validate_title_filter(title)
        code, out, _ = run_command(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_title}"])
        if code != 0 or not out:
            return None
        for line in out.splitlines():
            parts = line.strip().split(maxsplit=2)
            if not parts:
                continue
            target = parts[0]
            cmd = parts[1] if len(parts) > 1 else ""
            pane_title = parts[2] if len(parts) > 2 else ""
            if (process.lower() in cmd.lower() or process.lower() in pane_title.lower()) and (
                not title or title.lower() in pane_title.lower()
            ):
                return target
        return None

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        target = self._find_target(process, title)
        if not target:
            return {"ok": False, "error": "matching tmux pane not found"}
        code, out, err = run_command(["tmux", "capture-pane", "-p", "-t", target, "-S", "-300"])
        if code != 0:
            return {"ok": False, "error": err or "tmux capture-pane failed"}
        return {
            "ok": True,
            "error": "",
            "win": target,
            "tab": target,
            "title": target,
            "busy": False,
            "wname": target,
            "hist": out,
        }

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        target = self._find_target(process, title)
        if not target:
            return False, "matching tmux pane not found"
        clean = re.sub(r"\s+", " ", payload).strip()
        code, out, err = run_command(["tmux", "send-keys", "-t", target, clean, "Enter"])
        return code == 0, "SENT" if code == 0 else err or out

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        target = self._find_target(process, title)
        if not target:
            return False, "matching tmux pane not found"
        tmux_key_map = {
            "tab": "Tab",
            "\t": "Tab",
            "enter": "Enter",
            "return": "Enter",
            "\r": "Enter",
            "\n": "Enter",
            "esc": "Escape",
            "escape": "Escape",
            "\x1b": "Escape",
            "ctrl+c": "C-c",
            "ctrl_c": "C-c",
            "ctrl+p": "C-p",
            "ctrl_p": "C-p",
            "ctrl+d": "C-d",
            "ctrl_d": "C-d",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "space": "Space",
            "backspace": "BSpace",
        }
        tmux_key = tmux_key_map.get(key.lower().strip(), key)
        code, out, err = run_command(["tmux", "send-keys", "-t", target, tmux_key])
        return code == 0, "SENT" if code == 0 else err or out


def get_backend(backend_name: str = "auto") -> BaseTerminalBackend:
    """Resolve terminal backend instance by name or environment."""
    choice = backend_name.lower().strip()
    if choice == "auto":
        if os.environ.get("TMUX") and shutil.which("tmux"):
            return TmuxBackend()
        if sys.platform == "darwin":
            return TerminalAppBackend()
        if shutil.which("tmux"):
            return TmuxBackend()
        return TerminalAppBackend()

    if choice in ("terminal", "terminal.app", "apple"):
        return TerminalAppBackend()
    if choice in ("iterm", "iterm2"):
        return ITerm2Backend()
    if choice == "tmux":
        return TmuxBackend()

    raise ValueError(f"Unknown terminal backend: {backend_name}. Available: auto, terminal, iterm2, tmux")


# Backward-compatible function wrappers
def terminal_tab(process: str, title: str | None = None) -> dict[str, str | bool]:
    """Inspect terminal tab using the default Terminal.app backend."""
    return TerminalAppBackend().get_tab(process, title)


def process_pids(process: str) -> list[int]:
    """Return PIDs matching process name."""
    return TerminalAppBackend().get_pids(process)


def send_to_terminal(process: str, title: str | None, payload: str) -> tuple[bool, str]:
    """Send text to Terminal.app tab."""
    return TerminalAppBackend().send(process, title, payload)


# ---------------------------------------------------------------------------
# Git Context Engine & Smart Nudges
# ---------------------------------------------------------------------------

@dataclass
class GitStatus:
    """Git workspace status snapshot."""

    is_repo: bool = False
    branch: str = ""
    dirty: bool = False
    modified_count: int = 0
    untracked_count: int = 0
    commits_ahead: int = 0
    open_prs_count: int = 0
    last_commit: str = ""
    summary: str = ""
    head: str = ""
    modified_files: tuple[str, ...] = ()


GIT_STATUS_TTL_SECONDS = 30.0
_GIT_STATUS_CACHE: dict[str, tuple[float, GitStatus]] = {}


def get_git_status(repo_dir: str = ".", ttl_seconds: float = GIT_STATUS_TTL_SECONDS) -> GitStatus:
    """Cached wrapper around :func:`_get_git_status_uncached` with a TTL per repository."""
    try:
        key = str(Path(repo_dir).resolve())
    except OSError:
        key = repo_dir
    now = time.monotonic()
    cached = _GIT_STATUS_CACHE.get(key)
    if cached is not None and now - cached[0] < ttl_seconds:
        return cached[1]
    status = _get_git_status_uncached(repo_dir)
    _GIT_STATUS_CACHE[key] = (now, status)
    return status


def _get_git_status_uncached(repo_dir: str = ".") -> GitStatus:
    """Inspect git repository status safely without mutating workspace."""
    try:
        code, out, _ = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
        if code != 0 or "true" not in out:
            return GitStatus(is_repo=False)

        branch_code, branch_out, _ = run_command(["git", "branch", "--show-current"], cwd=repo_dir)
        branch = branch_out.strip() if branch_code == 0 else ""

        head_code, head_out, _ = run_command(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        head = head_out.strip() if head_code == 0 else ""

        _status_code, status_out, _ = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
        status_lines = [line for line in status_out.splitlines() if line.strip() and not line.strip().endswith(".DS_Store")]
        dirty = len(status_lines) > 0
        untracked = sum(1 for line in status_lines if line.startswith("??"))
        modified = len(status_lines) - untracked
        modified_files = tuple(line[3:].strip() if len(line) > 3 else line.strip() for line in status_lines)

        log_code, log_out, _ = run_command(["git", "log", "-n", "1", "--oneline"], cwd=repo_dir)
        last_commit = log_out.strip() if log_code == 0 else ""

        open_prs = 0
        if shutil.which("gh"):
            gh_code, gh_out, _ = run_command(["gh", "pr", "list", "--state", "open", "--json", "number"], cwd=repo_dir)
            if gh_code == 0:
                with contextlib.suppress(Exception):
                    open_prs = len(json.loads(gh_out))

        summary = f"branch={branch} dirty={dirty} mod={modified} untracked={untracked} prs={open_prs}"
        return GitStatus(
            is_repo=True,
            branch=branch,
            head=head,
            dirty=dirty,
            modified_count=modified,
            untracked_count=untracked,
            modified_files=modified_files,
            commits_ahead=0,
            open_prs_count=open_prs,
            last_commit=last_commit,
            summary=summary,
        )
    except Exception:
        return GitStatus(is_repo=False)


def generate_smart_nudge(git_status: GitStatus, default_nudge: str = "") -> str:
    """Generate a context-aware nudge based on git repository status."""
    if not git_status.is_repo:
        return default_nudge or "Continue with the next task according to the plan."

    if git_status.dirty:
        return (
            f"You have uncommitted changes on branch '{git_status.branch}'. "
            "Execute targeted tests for the current task, verify everything passes, and commit your changes."
        )

    if git_status.open_prs_count > 0:
        return "Check the status of open Pull Requests and CI checks, and proceed with merging once all checks pass."

    if git_status.branch not in ("main", "master", ""):
        return (
            f"Your working tree on branch '{git_status.branch}' is clean. "
            "Run full verification, push your branch, create a Pull Request and merge it."
        )

    return default_nudge or "Continue with the next task according to the plan."


# ---------------------------------------------------------------------------
# Classification and Decision Engine
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|token)\b\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{12,})"),
    re.compile(r"\b(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"(?i)([?&](?:token|key|secret|password|signature)=)([^&\s]+)"),
)


def redact_sensitive(text: str) -> str:
    """Mask common credentials before terminal history leaves the local monitor."""
    redacted = str(text)
    for pattern in SENSITIVE_VALUE_PATTERNS:
        if pattern.groups >= 2 and (
            pattern.pattern.startswith("(?i)(\\b") or "Bearer" in pattern.pattern or "(?:token|key" in pattern.pattern
        ):
            redacted = pattern.sub(lambda match: f"{match.group(1)}<redacted>", redacted)
        else:
            redacted = pattern.sub("<redacted>", redacted)
    return redacted


TODO_ITEM_PATTERN = re.compile(r"(?P<marker>\[(?:\s|x|X|•|·|✓|✔|~|-)\])\s*(?P<label>.+?)\s*$")
TODO_COMPLETION_RATIO_PATTERN = re.compile(
    r"(?<!\d)(?P<completed>\d+)\s*/\s*(?P<total>\d+)"
    r"(?:\s*(?:tasks?|tarefas?))?\s*"
    r"(?P<status>complete(?:d)?|conclu[ií]d[ao]s?|feitas?|finished|done)\b",
    re.IGNORECASE,
)
TODO_ALL_COMPLETE_PATTERN = re.compile(
    r"\b(?:all\s+(?:of\s+the\s+)?tasks?|todas?(?:\s+as)?\s+tarefas?|todos?(?:\s+os)?\s+tasks?)\b"
    r".{0,80}\b(?:complete(?:d)?|done|conclu[ií]d[ao]s?|feitas?)\b",
    re.IGNORECASE,
)
TODO_NO_PENDING_PATTERN = re.compile(
    r"\b(?:no\s+(?:pending|remaining)\s+tasks?|nenhuma?\s+pendente(?:s)?|não\s+há\s+(?:tarefas?\s+)?pendente(?:s)?)\b",
    re.IGNORECASE,
)
TODO_FINAL_COMPLETE_PATTERN = re.compile(
    r"\b(?:estado\s+final|final\s+state)\b.{0,160}\b(?:complete|completed|conclu[ií]d[ao]s?|feitas?|finished|done)\b",
    re.IGNORECASE,
)


def _explicit_todo_completion(history: str, marker_total: int) -> dict[str, Any] | None:
    """Prefer an agent's explicit final task summary over a stale TUI todo pane.

    OpenCode renders the conversation and its Todo side pane on the same
    terminal history line.  After an agent reports a completed ForgeLoop plan,
    the side pane can still contain the old ``[ ]`` markers, so counting every
    marker would regress from a verified ``35/35 COMPLETE`` result to a stale
    ``0/9`` view.  Only affirmative, non-question lines are accepted here; a
    question such as ``todas as tarefas foram feitas?`` must not close work.
    """
    lines = [re.sub(r"\s+", " ", line).strip(" │┃") for line in str(history).splitlines()]
    for line in reversed(lines[-200:]):
        if not line or "?" in line:
            continue

        ratio = TODO_COMPLETION_RATIO_PATTERN.search(line)
        if ratio:
            completed = int(ratio.group("completed"))
            total = int(ratio.group("total"))
            if total > 0 and completed == total:
                return {
                    "total": total,
                    "completed": total,
                    "in_progress": 0,
                    "pending": 0,
                    "items": [],
                    "source": "explicit_summary",
                    "evidence": redact_sensitive(line)[:240],
                }

        if marker_total == 0 and TODO_FINAL_COMPLETE_PATTERN.search(line):
            return {
                "total": 1,
                "completed": 1,
                "in_progress": 0,
                "pending": 0,
                "items": [],
                "source": "explicit_summary",
                "evidence": redact_sensitive(line)[:240],
            }

        if (TODO_ALL_COMPLETE_PATTERN.search(line) or TODO_NO_PENDING_PATTERN.search(line)) and marker_total:
            return {
                "total": marker_total,
                "completed": marker_total,
                "in_progress": 0,
                "pending": 0,
                "items": [],
                "source": "explicit_summary",
                "evidence": redact_sensitive(line)[:240],
            }
    return None


def extract_todo_progress(history: str) -> dict[str, Any]:
    """Extract task progress, preferring a current explicit summary to TUI markers."""
    items: dict[str, dict[str, str]] = {}
    for line in str(history).splitlines():
        match = TODO_ITEM_PATTERN.search(line)
        if not match:
            continue
        label = re.sub(r"\s+", " ", match.group("label")).strip(" │┃")
        if not label:
            continue
        marker = match.group("marker").lower()
        state = "completed" if marker in {"[x]", "[✓]", "[✔]"} else "in_progress" if marker in {"[•]", "[·]", "[~]", "[-]"} else "pending"
        key = label.rstrip("+").strip().lower()
        existing = items.get(key)
        priority = {"pending": 0, "in_progress": 1, "completed": 2}
        if existing is None or priority[state] > priority[existing["state"]]:
            items[key] = {"label": label.rstrip("+").strip(), "state": state}
    ordered = list(items.values())
    counts = {
        "total": len(ordered),
        "completed": sum(item["state"] == "completed" for item in ordered),
        "in_progress": sum(item["state"] == "in_progress" for item in ordered),
        "pending": sum(item["state"] == "pending" for item in ordered),
    }
    explicit = _explicit_todo_completion(history, counts["total"])
    if explicit:
        return explicit
    return {**counts, "items": ordered, "source": "tui_markers", "evidence": ""}


def infer_current_task_id(history: str) -> str:
    """Infer a task identifier from recent commands without mutating durable state."""
    patterns = (
        r"--task\s+([A-Za-z0-9][A-Za-z0-9_.:-]*)",
        r"\btask(?:\s+id)?\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9_.:-]*)",
    )
    for line in reversed(str(history).splitlines()):
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                return match.group(1)
    return ""


def redact_snapshot(snapshot: str, *, max_chars: int = 6000) -> tuple[str, bool]:
    """Return a bounded, credential-masked snapshot for human/JSON inspection."""
    safe = redact_sensitive(str(snapshot))
    truncated = len(safe) > max_chars
    return (safe[-max_chars:] if truncated else safe), truncated


def pid_is_alive(pid: int | str | None) -> bool:
    """Return whether a local process exists without treating permission as absence."""
    try:
        value = int(pid or 0)
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


ANSI_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "white": "\033[37m",
}


def _ansi(text: str, tone: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    return f"{ANSI_CODES.get(tone, '')}{text}{ANSI_CODES['reset']}"


def read_status_snapshot(state_dir: str | Path, project_dir: str = ".") -> dict[str, Any]:
    """Read live status plus durable state and mark stale monitor PIDs explicitly."""
    state_path = Path(state_dir)
    status_path = state_path / "status.json"
    task_path = state_path / "task-state.json"
    status: dict[str, Any] = {}
    task: dict[str, Any] = {}
    for path, target in ((status_path, status), (task_path, task)):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                target.update(loaded)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if not status:
        status = {
            "running": False,
            "state": "stopped",
            "timestamp": "",
            "git": {},
            "task": {},
        }
    monitor_pid = status.get("monitor_pid") or status.get("pid")
    pid_alive = bool(status.get("running") and pid_is_alive(monitor_pid))
    identity_checked = bool(status.get("monitor_instance_id") or status.get("schema_version", 1) >= 2)
    status["monitor_alive"] = bool(pid_alive and (not identity_checked or _monitor_process_matches(monitor_pid, state_path)))
    status["stale"] = bool(status.get("running") and monitor_pid and not status["monitor_alive"])
    if task:
        status["durable_task_state"] = task
        task_view = dict(status.get("task") or {})
        task_view.setdefault("task_id", task.get("taskId", ""))
        task_view.setdefault("stage", task.get("lastKnownStage", "TASK_RECEIVED"))
        task_view.setdefault("npm_publish_allowed", task.get("npmPublishAllowed", False))
        status["task"] = task_view
    if not status.get("git"):
        git = get_git_status(project_dir, ttl_seconds=0.0)
        status["git"] = {
            "branch": git.branch,
            "head": git.head,
            "dirty": git.dirty,
            "modified": git.modified_count,
            "untracked": git.untracked_count,
            "open_prs": git.open_prs_count,
            "last_commit": git.last_commit,
        }
    return status


def _status_monitor_label(snapshot: dict[str, Any]) -> tuple[str, str]:
    if snapshot.get("monitor_alive"):
        return "RUNNING", "green"
    if snapshot.get("stale"):
        return "STALE", "red"
    return "STOPPED", "yellow"


def render_status_dashboard(snapshot: dict[str, Any], *, color: bool = True, width: int = 78) -> str:
    """Render a compact ANSI dashboard suitable for a terminal or CI log."""
    monitor_label, monitor_tone = _status_monitor_label(snapshot)
    state = str(snapshot.get("state", "unknown")).upper()
    state_tone = "green" if state in {"THINKING", "COMPLETED"} else "yellow" if state in {"IDLE", "PERMISSION", "QUESTION"} else "red" if state in {"MISSING", "ATTENTION", "STOPPED"} else "cyan"
    task = snapshot.get("task") if isinstance(snapshot.get("task"), dict) else {}
    todo = snapshot.get("todo") if isinstance(snapshot.get("todo"), dict) else {}
    activity = snapshot.get("activity") if isinstance(snapshot.get("activity"), dict) else {}
    git = snapshot.get("git") if isinstance(snapshot.get("git"), dict) else {}
    ci_events = snapshot.get("ci_events") if isinstance(snapshot.get("ci_events"), list) else []
    ci_categories = [str(item.get("category", "unknown")) for item in ci_events[-8:] if isinstance(item, dict)]
    ci_label = "green" if ci_categories and all(item == "passed" for item in ci_categories) else "attention" if any(item in {"failed", "failed-external"} for item in ci_categories) else "waiting" if ci_categories else "not observed"
    ci_tone = "green" if ci_label == "green" else "red" if ci_label == "attention" else "yellow" if ci_label == "waiting" else "dim"
    npm_allowed = bool(snapshot.get("npm_publish_allowed", task.get("npm_publish_allowed", False)))
    npm_label = "ALLOWED" if npm_allowed else "BLOCKED"
    npm_tone = "red" if npm_allowed else "green"
    branch = str(git.get("branch") or "-")
    head = str(git.get("head") or "")[:12] or "-"
    dirty = bool(git.get("dirty"))
    current_command = ""
    commands = activity.get("commands")
    if isinstance(commands, list) and commands:
        current_command = redact_sensitive(str(commands[0]))
    todo_total = int(todo.get("total", 0) or 0)
    todo_completed = int(todo.get("completed", 0) or 0)
    todo_pending = int(todo.get("pending", 0) or 0) + int(todo.get("in_progress", 0) or 0)
    progress = f"{todo_completed}/{todo_total}" if todo_total else "not detected"
    task_id = str(task.get("detected_id") or task.get("task_id") or "-")
    state_fragment = _ansi(state, state_tone, color)
    mode = str(snapshot.get("mode") or "-")
    lines = [
        _ansi("╭─ AI Agent Terminal Monitor", "cyan", color),
        _ansi(f"│ Monitor   {monitor_label:<7}  Agent ", monitor_tone, color) + f"{state_fragment:<10}" + _ansi(f"  Mode {mode!s:<6}", monitor_tone, color),
        _ansi(f"│ Heartbeat {snapshot.get('heartbeat') or snapshot.get('timestamp') or '-'}", "dim", color),
        _ansi("├─ Task", "blue", color),
        f"│ {task_id}  ·  progress {progress}  ·  remaining {todo_pending}",
        f"│ Stage     {task.get('stage') or snapshot.get('stage') or '-'}",
        _ansi("├─ Repository", "blue", color),
        f"│ {branch} @ {head}  ·  {'DIRTY' if dirty else 'clean'}  ·  open PRs {git.get('open_prs', 0)}",
        _ansi("├─ Safety & CI", "blue", color),
        f"│ npm { _ansi(npm_label, npm_tone, color) }  ·  CI { _ansi(ci_label, ci_tone, color) }",
    ]
    if current_command:
        wrapped = textwrap.wrap(current_command, width=max(20, width - 4)) or [current_command]
        lines.append(_ansi("├─ Current command", "blue", color))
        lines.extend(f"│ {line}" for line in wrapped[:3])
    else:
        lines.append(_ansi("├─ Current command", "blue", color))
        lines.append("│ no child command observed")
    if snapshot.get("stale"):
        lines.append(_ansi("│ Monitor PID is no longer alive; status file is stale.", "red", color))
    lines.append(_ansi("╰────────────────────────────────────────────────────────────────────────────", "cyan", color))
    return "\n".join(lines)


def _monitor_process_matches(pid: int | str | None, state_dir: str | Path) -> bool:
    """Verify a PID belongs to this monitor before sending it a signal."""
    if not pid_is_alive(pid):
        return False
    code, output, _ = run_command(["ps", "-p", str(pid), "-o", "command="])
    if code != 0:
        return False
    command = output.lower()
    state_candidates = {str(Path(state_dir)).lower(), str(Path(state_dir).resolve()).lower()}
    return "terminal_monitor.py" in command and any(candidate in command for candidate in state_candidates)


def stop_monitor(state_dir: str | Path, *, reason: str = "cli_stop") -> dict[str, Any]:
    """Stop only a verified monitor process and leave the agent process untouched."""
    state_path = Path(state_dir)
    status = read_status_snapshot(state_path)
    pid = status.get("monitor_pid") or status.get("pid")
    stop_path = state_path / "stop"
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.touch()
    result: dict[str, Any] = {"ok": True, "action": "stop", "state_dir": str(state_path), "pid": pid, "agent_untouched": True}
    if _monitor_process_matches(pid, state_path):
        try:
            os.kill(int(pid), signal.SIGTERM)
            result.update({"signal": "SIGTERM", "reason": reason})
        except OSError as exc:
            result.update({"ok": False, "error": str(exc)})
    else:
        result.update({"signal": "none", "reason": "no verified live monitor"})
    return result


def resume_monitor(state_dir: str | Path, *, project_dir: str = ".") -> dict[str, Any]:
    """Resume a previously launched supervisor from its validated launch metadata."""
    state_path = Path(state_dir)
    status = read_status_snapshot(state_path, project_dir)
    if status.get("monitor_alive"):
        return {"ok": True, "action": "resume", "already_running": True, "pid": status.get("monitor_pid")}
    metadata_path = state_path / "monitor.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "action": "resume", "error": f"launch metadata unavailable: {exc}"}
    command = [str(item) for item in metadata.get("command", [])] if isinstance(metadata, dict) else []
    state_candidates = {str(state_path).lower(), str(state_path.resolve()).lower()}
    command_text = " ".join(command).lower()
    trusted_entrypoint = "terminal_monitor.py" in command_text or "supervisor.py" in command_text
    if not command or not trusted_entrypoint or ("supervise" not in command_text and "--supervise" not in command_text) or not any(candidate in command_text for candidate in state_candidates):
        return {"ok": False, "action": "resume", "error": "refusing untrusted monitor launch metadata"}
    with contextlib.suppress(OSError):
        (state_path / "stop").unlink()
    try:
        process = subprocess.Popen(command, cwd=project_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return {"ok": False, "action": "resume", "error": str(exc)}
    return {"ok": True, "action": "resume", "pid": process.pid, "command": command, "agent_untouched": True}


def normalize_snapshot(history: str) -> str:
    """Clean and normalize history snapshot for state hashing."""
    text = re.sub(r"[ \t]+", " ", history)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-30:])


def classify_state(
    history: str,
    profile: AgentProfile | None = None,
    *,
    activity: ProcessActivity | None = None,
    session_tracker: SessionTracker | None = None,
) -> str:
    """Classify the current terminal state (permission, question, completed, thinking, idle).

    Actionable states (permission/question/completed) take precedence over
    "thinking" because agents often keep spinner hints like "esc to cancel"
    visible while a permission prompt is on screen.
    """
    prof = profile or BUILTIN_PROFILES["opencode"]
    tail = "\n".join(history.splitlines()[-50:])

    if prof.matches_permission(tail):
        return "permission"
    # A real child command still owns the prompt; otherwise questions and
    # menus remain actionable even when Terminal.app reports the tab busy.
    if prof.matches_question(tail) and (not activity or not activity.active or not (activity.commands or activity.descendants)):
        return "question"

    active_child = bool(activity and (activity.commands or activity.descendants))
    completion_history = session_tracker.current_segment(history) if session_tracker and session_tracker.interaction_history else history
    completion = bool(_explicit_todo_completion(completion_history, marker_total=0))
    if session_tracker:
        completion = completion or session_tracker.matches_current_completion(history, prof.completion_patterns)
    else:
        completion = completion or prof.matches_completion(tail)
    if completion and not active_child and not (activity and activity.git_changed):
        return "completed"
    if activity and activity.active:
        return "thinking"
    if prof.matches_thinking(tail):
        return "thinking"
    return "idle"


def clean_option(line: str) -> str:
    """Strip menu bullets, numbers, and recommended markers from an option string."""
    value = re.sub(r"^[\s│┃>*●○◉❯-]+", "", line).strip()
    value = re.sub(r"^\d+[.)\]]\s*", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s*\(recommended\)\s*", "", value, flags=re.IGNORECASE).strip()


def extract_options(history: str, profile: AgentProfile | None = None) -> list[tuple[str, bool]]:
    """Extract selectable options and their recommendation status from history."""
    prof = profile or BUILTIN_PROFILES["opencode"]
    tail = "\n".join(history.splitlines()[-60:])
    return prof.extract_options(tail)


def is_unsafe(value: str, unsafe_list: list[str] | tuple[str, ...] | None = None) -> bool:
    """Check if an option or command string matches known unsafe phrases."""
    phrases = unsafe_list if unsafe_list is not None else UNSAFE_PHRASES
    low = value.lower()
    return any(phrase.lower() in low for phrase in phrases)


def decide_question(history: str, profile: AgentProfile | None = None) -> str | None:
    """Select the best safe option if unambiguous, otherwise None."""
    prof = profile or BUILTIN_PROFILES["opencode"]
    options = [
        (value, recommended)
        for value, recommended in extract_options(history, prof)
        if not is_unsafe(value, prof.unsafe_phrases)
    ]
    if not options:
        return None

    # Pick recommended safe option
    for value, recommended in options:
        if recommended:
            return value

    # Pick preferred keywords
    for key in prof.preferred_answers:
        for value, _ in options:
            if key.lower() in value.lower():
                return value

    # Pick if only one safe option exists
    return options[0][0] if len(options) == 1 else None


def append_log(path: str, message: str) -> None:
    """Append a timestamped log line to file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{now_iso()} {message}\n")


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Command Center</title><style>
:root{color-scheme:dark;--orange:#fe6e00;--bg:#0b0908;--panel:rgba(25,22,20,.82);--line:rgba(255,255,255,.12);--text:#fafaf9;--muted:#b9b3ac;--green:#00c758;--yellow:#edb200;--red:#fb2c36;--blue:#3080ff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0,rgba(254,110,0,.13),transparent 34%),var(--bg);color:var(--text);font:14px ui-sans-serif,system-ui,sans-serif;min-height:100vh}.shell{display:grid;grid-template-columns:230px 1fr;min-height:100vh}.side{padding:26px 20px;border-right:1px solid var(--line);background:rgba(0,0,0,.7);backdrop-filter:blur(18px)}.brand{font-weight:800;letter-spacing:.11em}.brand b{color:var(--orange)}.nav{margin-top:38px;color:var(--muted);line-height:2.8}.nav .active{color:#fff;border-left:2px solid var(--orange);padding-left:12px}.main{padding:28px;min-width:0}.top{display:flex;justify-content:space-between;align-items:end;margin-bottom:20px}.eyebrow{font:700 11px ui-monospace,monospace;color:var(--orange);letter-spacing:.15em}.top h1{font-size:27px;margin:6px 0 0}.live{font:700 11px ui-monospace,monospace;color:var(--green)}.live:before{content:'●';margin-right:7px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}.card,.terminal{border:1px solid var(--line);background:var(--panel);backdrop-filter:blur(16px);border-radius:8px}.card{padding:15px}.label{font:700 10px ui-monospace,monospace;color:var(--muted);letter-spacing:.12em}.value{font-size:18px;font-weight:750;margin-top:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.accent{color:var(--orange)}.terminal{height:calc(100vh - 180px);min-height:430px;display:flex;flex-direction:column}.terminal-head{padding:12px 15px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;font:700 11px ui-monospace,monospace}.dots{color:var(--orange);letter-spacing:4px}.log{padding:16px;overflow:auto;white-space:pre-wrap;word-break:break-word;font:13px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;flex:1}.line{color:var(--muted)}.line.info{color:var(--blue)}.line.ok{color:var(--green)}.line.warn{color:var(--yellow)}.line.bad{color:var(--red)}.line.action{color:var(--orange)}.empty{color:#797067}@media(max-width:850px){.shell{grid-template-columns:1fr}.side{display:none}.cards{grid-template-columns:repeat(2,1fr)}.main{padding:18px}.terminal{height:calc(100vh - 230px)}}
</style></head><body><div class="shell"><aside class="side"><div class="brand"><b>◉</b> AGENT // CENTER</div><div class="nav"><div class="active">Live Operations</div><div>Process Activity</div><div>Safety Events</div><div>Attempt Ledger</div></div></aside><main class="main"><div class="top"><div><div class="eyebrow">AUTONOMOUS OPERATIONS CONSOLE</div><h1>Terminal Monitor</h1></div><div class="live" id="connection">LIVE</div></div><section class="cards"><div class="card"><div class="label">AGENT STATE</div><div class="value accent" id="state">—</div></div><div class="card"><div class="label">PROCESS</div><div class="value" id="process">—</div></div><div class="card"><div class="label">TASK PROGRESS</div><div class="value" id="progress">—</div></div><div class="card"><div class="label">GIT BRANCH</div><div class="value" id="branch">—</div></div></section><section class="terminal"><div class="terminal-head"><span>LIVE OPERATIONAL LOG</span><span class="dots">● ● ●</span></div><div class="log" id="log"><span class="empty">Waiting for monitor events…</span></div></section></main></div><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function tone(s){s=s.toUpperCase();if(/SUCCESS|COMPLETED|GREEN|MERGED/.test(s))return'ok';if(/ATTENTION|PAUSE|WARN|QUEUED/.test(s))return'warn';if(/FAILED|ERROR|BLOCKED|REFUSED/.test(s))return'bad';if(/SEND|MODE|START|INTERRUPT|RECOVER/.test(s))return'action';return'info'}
async function tick(){try{const [sr,er]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/events',{cache:'no-store'})]);const s=await sr.json(),e=await er.json();state.textContent=s.state||'starting';process.textContent=(s.process||'agent')+' · '+((s.pids||[]).length)+' pid';const t=s.todo||{};progress.textContent=(t.completed||0)+' / '+(t.total||0);branch.textContent=(s.git||{}).branch||'—';log.innerHTML=(e.lines||[]).map(x=>'<div class="line '+tone(x)+'">'+esc(x)+'</div>').join('')||'<span class="empty">Waiting for monitor events…</span>';log.scrollTop=log.scrollHeight;connection.textContent='LIVE'}catch(e){connection.textContent='RECONNECTING'}}setInterval(tick,1000);tick();
</script></body></html>"""


class MonitorWebServer:
    """Local read-only dashboard serving status and the bounded event log."""

    def __init__(self, status_path: str, log_path: str, port: int = 8765) -> None:
        self.status_path = status_path
        self.log_path = log_path
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/":
                    self._reply(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode())
                elif self.path == "/api/status":
                    try:
                        payload = Path(owner.status_path).read_bytes()
                        json.loads(payload)
                    except (OSError, ValueError, json.JSONDecodeError):
                        payload = b'{"state":"starting","pids":[]}'
                    self._reply(200, "application/json", payload)
                elif self.path == "/api/events":
                    try:
                        lines = Path(owner.log_path).read_text(encoding="utf-8").splitlines()[-400:]
                    except OSError:
                        lines = []
                    self._reply(200, "application/json", json.dumps({"lines": lines}).encode())
                else:
                    self._reply(404, "application/json", b'{"error":"not found"}')

            def _reply(self, status: int, content_type: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="terminal-monitor-web", daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}/"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)


def consume_manual_answer(path: str) -> str | None:
    """Read and delete a manual answer file if present."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        os.remove(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return value or None


# ---------------------------------------------------------------------------
# Config Loading (JSON, TOML, Project Discovery)
# ---------------------------------------------------------------------------

CONFIG_FILENAMES = (
    ".terminal-monitor.json",
    ".terminal-monitor.toml",
    "terminal-monitor.json",
    "terminal-monitor.toml",
)


def discover_config_file(project_dir: str = ".") -> Path | None:
    """Look for standard config files in project_dir or user home."""
    base = Path(project_dir).resolve()
    for name in CONFIG_FILENAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate

    # Check user global config
    user_config_dir = Path.home() / ".config" / "terminal-monitor"
    for name in ("config.json", "config.toml"):
        candidate = user_config_dir / name
        if candidate.is_file():
            return candidate

    return None


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Parse a JSON or TOML configuration file."""
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {file_path}")

    text = file_path.read_text(encoding="utf-8")
    ext = file_path.suffix.lower()

    if ext == ".json":
        return json.loads(text)
    if ext == ".toml":
        if tomllib is None:
            raise RuntimeError("TOML support requires Python 3.11+ or the 'tomli' package.")
        return tomllib.loads(text)

    # Try JSON first, then TOML
    try:
        return json.loads(text)
    except Exception as json_err:
        if tomllib is not None:
            try:
                return tomllib.loads(text)
            except Exception:
                pass
        raise ValueError(f"Could not parse configuration file as JSON or TOML: {file_path}") from json_err


def generate_starter_config(format_type: str = "json") -> str:
    """Generate a starter configuration template in JSON or TOML."""
    if format_type.lower() == "json":
        return json.dumps(
            {
                "profile": "opencode",
                "process": "opencode",
                "continue_text": "Continue with the next task according to the plan.",
                "poll_seconds": 3.0,
                "idle_seconds": 15.0,
                "cooldown_seconds": 20.0,
                "gone_seconds": 25.0,
                "max_sends": 100,
                "auto_allow_permissions": True,
                "supervise": True,
                "smart_nudges": True,
                "auto_switch_modes": True,
                "completion_check": True,
                "objective": "Finish every task, create and merge the PR, then verify main.",
                "prohibitions": ["Do not publish to npm."],
                "task_id": "example-task",
                "required_outcome": "merged",
                "npm_publish_allowed": False,
                "session_id": "",
                "expected_branch": "",
                "protected_branches": ["main", "master"],
                "report_path": "",
                "attempt_history_limit": 100,
                "loop_guard": True,
                "loop_repeat_limit": 3,
                "queued_attempt_seconds": 45.0,
                "allow_history_rewrite": False,
                "web_ui": True,
                "web_port": 8765,
                "web_open_browser": True,
                "unsafe_phrases": list(UNSAFE_PHRASES),
                "custom_profiles": {
                    "my-agent": {
                        "process": "myagent",
                        "description": "Custom agent CLI configuration",
                        "thinking_patterns": ["agent is thinking...", "processing..."],
                        "permission_patterns": ["do you authorize this action?"],
                        "auto_permission_payload": "y",
                        "mode_patterns": {"plan": "Plan Mode", "build": "Build Mode"},
                        "completion_patterns": ["all tasks complete"],
                    }
                },
            },
            indent=2,
        )
    elif format_type.lower() == "toml":
        return """profile = "opencode"
process = "opencode"
continue_text = "Continue with the next task according to the plan."
poll_seconds = 3.0
idle_seconds = 15.0
cooldown_seconds = 20.0
max_sends = 100
auto_allow_permissions = true
supervise = true
smart_nudges = true
auto_switch_modes = true
completion_check = true
objective = "Finish every task, create and merge the PR, then verify main."
prohibitions = ["Do not publish to npm."]
task_id = "example-task"
required_outcome = "merged"
npm_publish_allowed = false
session_id = ""
expected_branch = ""
protected_branches = ["main", "master"]
report_path = ""
attempt_history_limit = 100
loop_guard = true
loop_repeat_limit = 3
queued_attempt_seconds = 45.0
allow_history_rewrite = false
web_ui = true
web_port = 8765
web_open_browser = true

unsafe_phrases = ["bypass", "delete", "rm -rf", "reset --hard"]

[custom_profiles.my-agent]
process = "myagent"
description = "Custom agent CLI configuration"
thinking_patterns = ["agent is thinking...", "processing..."]
permission_patterns = ["do you authorize this action?"]
auto_permission_payload = "y"
completion_patterns = ["all tasks complete"]
"""
    else:
        raise ValueError(f"Unsupported format: {format_type}. Use 'json' or 'toml'.")


# ---------------------------------------------------------------------------
# TerminalMonitor Core Engine (Class API)
# ---------------------------------------------------------------------------

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

        # Setup state paths
        self.state_dir = config.state_dir
        os.makedirs(self.state_dir, exist_ok=True)
        self.log_path = os.path.join(self.state_dir, "monitor.log")
        self.attention_path = os.path.join(self.state_dir, "attention.txt")
        self.answer_path = os.path.join(self.state_dir, "answer.txt")
        self.stop_path = os.path.join(self.state_dir, "stop")
        self.monitor_lock_path = os.path.join(self.state_dir, "monitor.pid")
        self.monitor_meta_path = os.path.join(self.state_dir, "monitor.json")
        self.status_json_path = config.status_json_path or os.path.join(self.state_dir, "status.json")
        self.task_state_path = os.path.join(self.state_dir, "task-state.json")
        self.report_path = config.report_path or os.path.join(self.state_dir, "final-report.json")
        stored_state = TaskState.load(self.task_state_path)
        current_git_status = get_git_status(config.project_dir, ttl_seconds=0.0)
        detected_branch = current_git_status.branch or stored_state.branch
        expected_branch = config.expected_branch or stored_state.expected_branch
        if config.supervise and not expected_branch:
            expected_branch = detected_branch
        self.task_state = replace(
            stored_state,
            objective=config.objective or stored_state.objective,
            prohibitions=tuple(config.prohibitions) or stored_state.prohibitions,
            task_id=config.task_id or stored_state.task_id,
            required_outcome=config.required_outcome or stored_state.required_outcome,
            npm_publish_allowed=config.npm_publish_allowed,
            session_id=config.session_id or stored_state.session_id,
            branch=detected_branch,
            expected_branch=expected_branch,
            report_path=self.report_path,
        )
        if config.supervise and not self.task_state.pr.get("safetyBaselineCaptured"):
            baseline = dict(self.task_state.pr)
            baseline.update(capture_safety_baseline(config.project_dir))
            self.task_state = replace(self.task_state, pr=baseline)
        self.task_state.save(self.task_state_path)
        self.session_tracker = SessionTracker(
            interaction_history=self.task_state.interaction_marker,
            generation=self.task_state.session_generation,
        )
        self.policy = PolicyEnvelope(self.task_state.objective, self.task_state.prohibitions)
        self.pr_machine = PullRequestStateMachine()
        self.pr_machine.stage = self.task_state.last_known_stage
        self.attempt_ledger = AttemptLedger(list(self.task_state.attempts), max_records=config.attempt_history_limit)
        self.agent_loop_guard = AgentLoopGuard(config.loop_repeat_limit)
        self.loop_assessment = LoopAssessment()

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
        self.todo_progress: dict[str, Any] = {"total": 0, "completed": 0, "in_progress": 0, "pending": 0, "items": []}
        self.detected_task_id = ""
        self.web_server: MonitorWebServer | None = None
        self.web_url = ""
        self._shutdown_requested = False
        self._shutdown_reason = ""
        self._previous_signal_handlers: dict[int, Any] = {}
        self._lock_claimed = False
        self._lifecycle = "stopped"

        # Callbacks
        self.on_state_change: Callable[[str, str], None] | None = None
        self.on_mode_change: Callable[[str | None, str | None], None] | None = None
        self.on_send: Callable[[str, str, bool], None] | None = None
        self.on_attention: Callable[[str, str], None] | None = None
        self.on_complete: Callable[[str], None] | None = None
        self.on_tick: Callable[[str, int], None] | None = None

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
        """Claim one supervisor per state directory, replacing only a stale lock."""
        try:
            existing = json.loads(Path(self.monitor_lock_path).read_text(encoding="utf-8"))
            existing_pid = existing.get("pid") if isinstance(existing, dict) else None
            if existing_pid and int(existing_pid) != os.getpid() and pid_is_alive(existing_pid):
                return False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        _atomic_json_write(self.monitor_lock_path, {"pid": os.getpid(), "instance_id": self.monitor_instance_id, "started_at": self.monitor_started_at})
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

    def _record_ci_events(self, classifications: list[dict[str, Any]]) -> None:
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
        started = float(latest.get("monotonic", time.monotonic()))
        age = max(0.0, time.monotonic() - started)
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
        reason = self.loop_assessment.reason
        evidence = ", ".join(self.loop_assessment.evidence) or "repeated command"
        instruction = (
            f"The monitor detected {reason} ({evidence}) and interrupted only the duplicated/stuck child command tree. "
            "Keep this agent session alive. Diagnose the cause, use targeted checks first, and do not relaunch the same full suite until Git or task progress changes."
        )
        payload = self.policy.compose(instruction, self.task_state.last_known_stage)
        attempt_id = self._queue_attempt("loop_recovery", payload, observed_state)
        ok, detail = self.backend.send(self.config.process, self.config.title, payload)
        self._transition_attempt(attempt_id, "sent", detail=detail, observed_state=observed_state)
        self._transition_attempt(attempt_id, "accepted" if ok else "ignored", detail=detail, observed_state=observed_state)
        if not ok:
            return False, "recovery_prompt_failed"
        self.agent_loop_guard.reset()
        self.last_send = time.monotonic()
        self.last_change = time.monotonic()
        self.sends += 1
        self.last_action = "loop_recovery:accepted"
        return True, f"interrupted={','.join(str(pid) for pid in interrupted)}"

    def _effective_threshold(self, state: str) -> float:
        """Idle seconds to wait before acting; actionable prompts act faster."""
        if self.config.idle_seconds == 0.0:
            return 0.0
        return 4.0 if state in ("permission", "question") else self.config.idle_seconds

    def log(self, message: str) -> None:
        append_log(self.log_path, message)

    def export_status_json(self, pids: list[int], state: str, extra: dict[str, Any] | None = None) -> None:
        """Export live structured status for IDEs, dashboards, or subagents."""
        if not self.status_json_path:
            return
        self.last_heartbeat = now_iso()
        git_status = get_git_status(self.config.project_dir, ttl_seconds=0.0)
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
            "history": {"available": True, "redacted": True, "max_chars": 6000},
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
        if todo_history.strip():
            self.todo_progress = extract_todo_progress(todo_history)
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

    def step(self) -> tuple[int | None, str]:
        """Perform a single monitor iteration.

        Returns (exit_code, status_message).
        If exit_code is None, the monitor should continue running.
        """
        if os.path.exists(self.stop_path):
            self._stop_status("stop_file")
            return 0, "CANCELLED"

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

        history = str(tab.get("hist", ""))
        snapshot = normalize_snapshot(history)
        safe_snapshot, _ = redact_snapshot(snapshot)
        digest = hashlib.sha256(snapshot.encode("utf-8", "replace")).hexdigest()[:16]
        activity = self._process_activity(pids, bool(tab.get("busy")))
        state = classify_state(history, self.profile, activity=activity, session_tracker=self.session_tracker)
        self._complete_observed_attempt(history, state)
        mode = self.profile.detect_mode(history)
        self.last_heartbeat = now_iso()
        if activity.commands:
            self.last_command = activity.commands[0]
        todo_history = self.session_tracker.current_segment(history) if self.session_tracker.interaction_history else history
        if todo_history.strip():
            self.todo_progress = extract_todo_progress(todo_history)
        self.detected_task_id = infer_current_task_id(history)
        self.last_action = f"observe:{state}"

        if mode != self.current_mode:
            if self.on_mode_change:
                self.on_mode_change(self.current_mode, mode)
            self.current_mode = mode

        if state != self.current_state:
            if self.on_state_change:
                self.on_state_change(self.current_state, state)
            self.current_state = state

        queued_attempt = self._stale_queued_attempt()
        if queued_attempt:
            if self.config.supervise:
                self._record_policy_decision(str(queued_attempt.get("attempt_id", "")), "attention", "queued_attempt_stale")
                Path(self.attention_path).write_text(json.dumps(queued_attempt, indent=2) + "\n" + safe_snapshot + "\n", encoding="utf-8")
                self.log(f"PAUSE kind=queued_attempt_stale age={queued_attempt['age_seconds']}")
                self.export_status_json(pids, "queued_attempt_stale", {"queued_attempt": queued_attempt})
                return 3, f"ATTENTION_REQUIRED kind=queued_attempt_stale file={self.attention_path}"
            return None, "WAITING_QUEUED_ATTEMPT"
        latest_attempt = self.attempt_ledger.latest(self.task_state.last_attempt_id or None)
        if latest_attempt and latest_attempt.get("status") == "queued" and latest_attempt.get("detail") == "terminal reports message queued":
            return None, "WAITING_QUEUED_ATTEMPT"

        if self.config.supervise:
            reference = str(self.task_state.pr.get("number") or self.task_state.branch or "")
            pr_snapshot = get_current_pr_snapshot(self.config.project_dir, reference)
            if pr_snapshot:
                classifications = [classify_check_result(check) for check in pr_snapshot.get("statusCheckRollup") or []]
                self._record_ci_events(classifications)
                stage = self.pr_machine.advance({
                    "number": pr_snapshot.get("number"),
                    "state": pr_snapshot.get("state"),
                    "checks": pr_snapshot.get("statusCheckRollup") or [],
                })
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

        git_status = get_git_status(self.config.project_dir)
        event_signature = "|".join(
            (
                state,
                str(mode or ""),
                ",".join(str(pid) for pid in pids),
                ",".join(str(pid) for pid in activity.descendants),
                str(self.todo_progress.get("completed", 0)),
                str(self.todo_progress.get("total", 0)),
                git_status.head,
                ",".join(activity.duplicate_commands),
            )
        )
        if event_signature != self.last_event_signature:
            self.last_event_signature = event_signature
            self.log(
                f"EVENT state={state} mode={mode or 'unknown'} roots={len(pids)} children={len(activity.descendants)} "
                f"task={self.todo_progress.get('completed', 0)}/{self.todo_progress.get('total', 0)} git={git_status.head[:8] or 'none'}"
            )
        repository_safety = evaluate_repository_safety(
            git_status,
            expected_branch=self.task_state.expected_branch,
            protected_branches=self.config.protected_branches,
        )
        if self.config.supervise and not repository_safety["safe"]:
            reason = str(repository_safety["reason"])
            self._record_policy_decision(
                f"branch={repository_safety.get('branch', '')}",
                "attention",
                reason,
            )
            with open(self.attention_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(repository_safety, indent=2) + "\n" + safe_snapshot + "\n")
            self.log(f"PAUSE kind=repository_safety reason={reason}")
            self.export_status_json(pids, "branch_safety", {"repository_safety": repository_safety})
            return 3, f"ATTENTION_REQUIRED kind=repository_safety reason={reason} file={self.attention_path}"

        immediate_assessment = assess_agent_commands(
            activity.commands,
            duplicate_commands=activity.duplicate_commands,
            allow_history_rewrite=self.config.allow_history_rewrite,
        )
        progress_fingerprint = f"{self.todo_progress.get('completed', 0)}/{self.todo_progress.get('total', 0)}"
        git_fingerprint = hashlib.sha256(
            f"{git_status.head}|{git_status.branch}|{git_status.modified_files}|{git_status.untracked_count}".encode("utf-8", "replace")
        ).hexdigest()[:16]
        repeated_assessment = self.agent_loop_guard.observe(
            digest,
            progress_fingerprint,
            git_fingerprint,
            git_status.head,
            activity.commands,
            episode=",".join(str(pid) for pid in activity.direct_descendants),
        )
        self.loop_assessment = immediate_assessment if immediate_assessment.detected else repeated_assessment
        if self.config.supervise and self.config.loop_guard and self.loop_assessment.detected:
            reason = self.loop_assessment.reason
            attention = {
                "kind": "agent_loop",
                "reason": reason,
                "evidence": list(self.loop_assessment.evidence),
                "occurrences": self.loop_assessment.occurrences,
                "activity": json_safe(activity),
            }
            self._record_policy_decision("agent_process_activity", "attention", reason)
            if reason in {"duplicate_expensive_commands", "repeated_expensive_command_without_progress"}:
                recovered, recovery_detail = self._recover_agent_loop(pids, activity, state)
                attention["recovery"] = {"ok": recovered, "detail": recovery_detail, "root_pids_protected": list(pids)}
                if recovered:
                    Path(self.attention_path).write_text(json.dumps(attention, indent=2) + "\n", encoding="utf-8")
                    self.log(f"RECOVER kind=agent_loop reason={reason} {recovery_detail}")
                    self.export_status_json(pids, "loop_recovered", {"loop_guard": attention})
                    return None, f"LOOP_RECOVERED reason={reason} {recovery_detail}"
            Path(self.attention_path).write_text(json.dumps(attention, indent=2) + "\n" + safe_snapshot + "\n", encoding="utf-8")
            self.log(f"PAUSE kind=agent_loop reason={reason}")
            self.export_status_json(pids, "agent_loop", {"loop_guard": attention})
            return 3, f"ATTENTION_REQUIRED kind=agent_loop reason={reason} file={self.attention_path}"

        self.export_status_json(pids, state, {
            "activity": {
                "active": activity.active,
                "descendants": list(activity.descendants),
                "direct_descendants": list(activity.direct_descendants),
                "commands": list(activity.commands),
                "cpu_percent": activity.cpu_percent,
                "oldest_seconds": activity.oldest_seconds,
                "git_changed": activity.git_changed,
                "duplicate_commands": list(activity.duplicate_commands),
                "expensive_roots": list(activity.expensive_roots),
            },
            "loop_guard": json_safe(self.loop_assessment),
            "task": {
                "task_id": self.task_state.task_id,
                "stage": self.task_state.last_known_stage,
                "session_generation": self.session_tracker.generation,
                "pr": self.task_state.pr,
                "npm_publish_allowed": self.task_state.npm_publish_allowed,
            },
            "repository_safety": repository_safety,
        })

        if self.config.once:
            return 0, f"STATE={state} MODE={mode} PID_COUNT={len(pids)}"

        if digest != self.last_digest:
            self.last_digest = digest
            self.last_change = time.monotonic()

        stable_for = time.monotonic() - self.last_change

        # A human/operator answer is an explicit instruction and therefore
        # outranks inferred UI state, spinner text, thresholds, and cooldowns.
        manual_answer = consume_manual_answer(self.answer_path)
        if manual_answer:
            allowed, policy_reason = self.policy.authorize_action(
                manual_answer,
                unsafe_phrases=tuple(dict.fromkeys([*UNSAFE_PHRASES, *self.config.unsafe_phrases])),
                npm_publish_allowed=self.task_state.npm_publish_allowed,
            )
            if not allowed:
                self._record_policy_decision(manual_answer, "blocked", policy_reason)
                with open(self.attention_path, "w", encoding="utf-8") as handle:
                    handle.write(safe_snapshot + "\n")
                self.log(f"PAUSE kind=policy_conflict reason={policy_reason}")
                return 3, f"ATTENTION_REQUIRED kind=policy_conflict file={self.attention_path}"
            attempt_id = self._queue_attempt("manual", manual_answer, state)
            if self.config.dry_run:
                self._transition_attempt(attempt_id, "ignored", detail="dry_run", observed_state=state)
                return 0, f"DRY_RUN kind=manual payload={manual_answer}"
            ok, detail = self.backend.send(self.config.process, self.config.title, manual_answer)
            self._transition_attempt(attempt_id, "sent", detail=detail, observed_state=state)
            self._transition_attempt(attempt_id, "accepted" if ok else "ignored", detail=detail, observed_state=state)
            self.export_status_json(pids, state, {"last_attempt_id": attempt_id})
            self.sends += 1
            self.last_send = time.monotonic()
            self.last_change = time.monotonic()
            self.last_action = f"send:manual:{'accepted' if ok else 'failed'}"
            self.session_tracker.mark_interaction(history)
            self.task_state = replace(
                self.task_state,
                session_generation=self.session_tracker.generation,
                interaction_marker=self.session_tracker.interaction_history,
            )
            self.task_state.save(self.task_state_path)
            self.log(f"SEND kind=manual n={self.sends} ok={ok} detail={detail}")
            if self.on_send:
                self.on_send("manual", manual_answer, ok)
            return (None, f"SENT kind=manual n={self.sends}") if ok else (1, f"SEND_FAILED kind=manual n={self.sends}")

        # Handle Completion State
        if state == "completed" and self.config.completion_check:
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
                    attempt_id = self._queue_attempt("final_verification", payload, state)
                    if self.config.dry_run:
                        self._transition_attempt(attempt_id, "ignored", detail="dry_run", observed_state=state)
                        return 0, "DRY_RUN kind=final_verification"
                    ok, detail = self.backend.send(self.config.process, self.config.title, payload)
                    self._transition_attempt(attempt_id, "sent", detail=detail, observed_state=state)
                    self._transition_attempt(attempt_id, "accepted" if ok else "ignored", detail=detail, observed_state=state)
                    self.last_send = time.monotonic()
                    self.sends += 1
                    self.last_action = f"send:final_verification:{'accepted' if ok else 'failed'}"
                    self.log(f"SEND kind=final_verification n={self.sends} ok={ok} detail={detail}")
                    return (None, "SENT kind=final_verification") if ok else (1, "SEND_FAILED kind=final_verification")
            self.log("SUCCESS: Completion indicators detected. Work complete.")
            if self.config.supervise:
                evidence = collect_final_evidence(self.config.project_dir, self.task_state)
                write_final_report(self.report_path, self.task_state, evidence, FinalVerificationReport(True, {"completion_detected": True}, ()))
            if self.on_complete:
                self.on_complete(snapshot)
            self.export_status_json(pids, "completed", {"done": True})
            return 0, "COMPLETED"

        threshold = self._effective_threshold(state)

        if state == "thinking" or stable_for < threshold:
            if self.on_tick:
                self.on_tick(state, len(pids))
            return None, f"WAITING state={state} stable_for={stable_for:.1f}"

        if time.monotonic() - self.last_send < self.config.cooldown_seconds:
            return None, "COOLDOWN"

        mode_threshold = self._effective_threshold("permission")
        # Handle Plan Mode Auto-Transition
        if self.config.auto_switch_modes and mode == "plan" and self.profile.is_plan_ready(history) and stable_for >= mode_threshold:
            self.log("MODE: Plan completed. Auto-switching mode via switch key.")
            self.last_action = "mode_switch:queued"
            attempt_id = self._queue_attempt("mode_switch", self.profile.mode_switch_key, state)
            if self.config.dry_run:
                self._transition_attempt(attempt_id, "ignored", detail="dry_run", observed_state=state)
                self._record_policy_decision("mode_switch", "ignored", "dry_run")
                return 0, "DRY_RUN kind=mode_switch"
            ok, detail = self.backend.send_key(self.config.process, self.config.title, self.profile.mode_switch_key)
            self._transition_attempt(attempt_id, "sent", detail=detail, observed_state=state)
            self._transition_attempt(attempt_id, "accepted" if ok else "ignored", detail=detail, observed_state=state)
            self.last_send = time.monotonic()
            self.last_change = time.monotonic()
            self.last_action = "mode_switch:accepted" if ok else "mode_switch:failed"
            if self.config.continue_text:
                time.sleep(0.5)
                self.backend.send(self.config.process, self.config.title, self.config.continue_text)
            return None, "MODE_SWITCH_SENT"

        payload: str | None = self.config.continue_text
        reason = "idle"

        if state == "permission":
            payload = self.profile.auto_permission_payload if self.config.auto_allow_permissions else None
            reason = "permission"
        elif state == "question":
            payload = decide_question(history, self.profile)
            reason = "question"
        elif state == "idle" and self.config.smart_nudges:
            git_info = get_git_status(self.config.project_dir)
            payload = generate_smart_nudge(git_info, self.config.continue_text)
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
                handle.write(safe_snapshot + "\n")
            if self.on_attention:
                self.on_attention(reason, safe_snapshot)
            self.log(f"PAUSE kind={reason} hash={digest}")
            return 3, f"ATTENTION_REQUIRED kind={reason} file={self.attention_path}"

        allowed, policy_reason = self.policy.authorize_action(
            payload,
            unsafe_phrases=tuple(dict.fromkeys([*UNSAFE_PHRASES, *self.config.unsafe_phrases])),
            npm_publish_allowed=self.task_state.npm_publish_allowed,
        )
        if not allowed:
            self._record_policy_decision(payload, "blocked", policy_reason)
            with open(self.attention_path, "w", encoding="utf-8") as handle:
                handle.write(safe_snapshot + "\n")
            self.log(f"PAUSE kind=policy_conflict reason={policy_reason}")
            return 3, f"ATTENTION_REQUIRED kind=policy_conflict file={self.attention_path}"

        attempt_id = self._queue_attempt(reason, payload, state)

        if self.config.dry_run:
            self._transition_attempt(attempt_id, "ignored", detail="dry_run", observed_state=state)
            return 0, f"DRY_RUN kind={reason} payload={payload or '<enter>'}"

        ok, detail = self.backend.send(self.config.process, self.config.title, payload)
        self._transition_attempt(attempt_id, "sent", detail=detail, observed_state=state)
        self._transition_attempt(attempt_id, "accepted" if ok else "ignored", detail=detail, observed_state=state)
        self.export_status_json(pids, state, {"last_attempt_id": attempt_id})
        self.sends += 1
        self.last_send = time.monotonic()
        self.last_action = f"send:{reason}:{'accepted' if ok else 'failed'}"
        self.log(f"SEND kind={reason} n={self.sends} ok={ok} detail={detail}")
        if ok:
            self.session_tracker.mark_interaction(history)
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

    def run(self) -> int:
        """Run monitor loop continuously until exit condition is met."""
        if not self._claim_monitor_lock():
            self.log("EXIT code=2 msg=MONITOR_ALREADY_RUNNING")
            return 2
        self.log(f"START process={self.config.process} profile={self.profile.name} backend={self.backend.name()}")
        if self.config.web_ui and not self.config.once:
            try:
                self.web_server = MonitorWebServer(self.status_json_path, self.log_path, self.config.web_port)
                self.web_url = self.web_server.start()
                self.log(f"WEB_UI url={self.web_url}")
                if self.config.web_open_browser:
                    webbrowser.open(self.web_url)
            except OSError as exc:
                self.log(f"WEB_UI_FAILED error={redact_sensitive(str(exc))}")
        self._install_shutdown_handlers()
        try:
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

    def _get_tab(self, pids: list[int]) -> dict[str, str | bool]:
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


# ---------------------------------------------------------------------------
# CLI Parser and Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build comprehensive CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="terminal_monitor",
        description="Monitor and safely nudge AI CLI coding agents running in Terminal.app, iTerm2, or tmux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # init config subcommand
    init_parser = subparsers.add_parser("init", help="Generate a starter configuration file")
    init_parser.add_argument("--format", choices=["json", "toml"], default="json", help="Configuration format (default: json)")
    init_parser.add_argument("-o", "--output", help="Output file path (default: .terminal-monitor.<format>)")

    # list profiles subcommand
    subparsers.add_parser("profiles", help="List built-in and discovered agent profiles")

    status_parser = subparsers.add_parser("status", help="Show a live colored monitor dashboard")
    status_parser.add_argument("--state-dir", default=None, help="Monitor state directory")
    status_parser.add_argument("--project-dir", "-d", default=None, help="Project directory for repository details")
    status_parser.add_argument("--json", action="store_true", dest="status_json", help="Print machine-readable JSON")
    status_parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    status_parser.add_argument("--watch", action="store_true", help="Refresh the dashboard continuously")
    status_parser.add_argument("--interval", type=float, default=2.0, help="Refresh interval for --watch")

    stop_parser = subparsers.add_parser("stop", help="Stop the monitor without interrupting the agent")
    stop_parser.add_argument("--state-dir", default=None, help="Monitor state directory")
    stop_parser.add_argument("--reason", default="cli_stop", help="Reason recorded for the stop")

    resume_parser = subparsers.add_parser("resume", help="Resume a monitor from saved launch metadata")
    resume_parser.add_argument("--state-dir", default=None, help="Monitor state directory")
    resume_parser.add_argument("--project-dir", "-d", default=None, help="Project directory for the monitor")

    send_parser = subparsers.add_parser("send", help="Send an explicit instruction to the selected agent terminal")
    send_parser.add_argument("text", help="Instruction text to send")
    _add_monitor_args(send_parser)

    interrupt_parser = subparsers.add_parser("interrupt-child", help="Interrupt only a verified child command")
    interrupt_parser.add_argument("--pid", type=int, required=True, help="Child process ID to interrupt")
    _add_monitor_args(interrupt_parser)

    restart_parser = subparsers.add_parser("restart-agent", help="Restart an agent using saved task state")
    restart_parser.add_argument("--continue-session", action="store_true", help="Pass the saved session identifier")
    restart_parser.add_argument("--agent-command", nargs=argparse.REMAINDER, help="Explicit agent command and arguments")
    _add_monitor_args(restart_parser)

    verify_parser = subparsers.add_parser("verify-final-state", help="Verify PR, branch, CI, release, and npm safety invariants")
    verify_parser.add_argument("--pr", type=int, help="Pull Request number (defaults to saved state/current branch)")
    _add_monitor_args(verify_parser)

    merge_parser = subparsers.add_parser("merge-pr", help="Merge a PR only after the exact-head green-check gate")
    merge_parser.add_argument("--pr", type=int, required=True, help="Pull Request number")
    merge_parser.add_argument("--head", required=False, help="Expected full 40-character PR head SHA (defaults to saved state)")
    _add_monitor_args(merge_parser)

    # supervise subcommand
    supervise_parser = subparsers.add_parser("supervise", help="Run autonomous supervision daemon")
    _add_monitor_args(supervise_parser)
    supervise_parser.add_argument("--status-json", dest="status_json_path", help="Path to write real-time status JSON")

    # Main monitor arguments
    _add_monitor_args(parser)
    parser.add_argument("--status-json", dest="status_json_path", help="Path to write real-time status JSON")

    return parser


def _add_monitor_args(parser: argparse.ArgumentParser) -> None:
    """Add standard monitor flags to a parser with None defaults so config files take effect."""
    parser.add_argument("--process", "-p", default=None, help="Agent process name to track (default: opencode)")
    parser.add_argument("--profile", default=None, help="Agent profile to use (e.g. claude, opencode, aider, goose)")
    parser.add_argument("--title", "-t", default=None, help="Window title substring filter")
    parser.add_argument("--continue-text", "-c", default=None, help="Text sent on idle or nudge")
    parser.add_argument("--continue-file", "-f", default=None, help="Path to file whose content is sent on idle")
    parser.add_argument("--poll-seconds", type=float, default=None, help="Loop interval (default: 3.0s)")
    parser.add_argument("--idle-seconds", type=float, default=None, help="Seconds before idle trigger (default: 15.0s)")
    parser.add_argument("--cooldown-seconds", type=float, default=None, help="Cooldown after sending (default: 20.0s)")
    parser.add_argument("--gone-seconds", type=float, default=None, help="Seconds before process gone (default: 25.0s)")
    parser.add_argument("--max-sends", type=int, default=None, help="Maximum sends before exit (default: 100)")
    parser.add_argument("--auto-allow-permissions", "-a", action="store_true", default=None, help="Auto-allow safe permission prompts")
    parser.add_argument("--supervise", "-S", action="store_true", default=None, help="Enable autonomous supervisor mode")
    parser.add_argument("--no-smart-nudges", action="store_true", help="Disable git-aware context smart nudges")
    parser.add_argument("--no-mode-switch", action="store_true", help="Disable automatic Plan->Build mode switching")
    parser.add_argument("--no-completion-check", action="store_true", help="Disable completion state auto-detection")
    parser.add_argument("--backend", "-b", choices=["auto", "terminal", "iterm2", "tmux"], default=None, help="Terminal backend")
    parser.add_argument("--project-dir", "-d", default=None, help="Project directory for config discovery and git status")
    parser.add_argument("--config", default=None, help="Explicit configuration file path")
    parser.add_argument("--state-dir", default=None, help="Directory for state/logs (default: /tmp/terminal-monitor)")
    parser.add_argument("--unsafe-phrase", action="append", dest="unsafe_phrases", help="Add custom unsafe phrases")
    parser.add_argument("--objective", default=None, help="Permanent task objective included with every nudge")
    parser.add_argument("--prohibition", action="append", dest="prohibitions", help="Permanent instruction that dynamic nudges cannot override")
    parser.add_argument("--task-id", default=None, help="Durable external task identifier")
    parser.add_argument("--required-outcome", default=None, help="Required final outcome (default: merged)")
    parser.add_argument("--allow-npm-publish", action="store_true", default=None, help="Explicitly allow npm publication (default: prohibited)")
    parser.add_argument("--session-id", default=None, help="Agent session identifier for robust selection/restart")
    parser.add_argument("--expected-branch", default=None, help="Pause supervision if the repository branch differs")
    parser.add_argument("--protected-branch", action="append", dest="protected_branches", help="Branch that must never be dirty during supervision")
    parser.add_argument("--report-path", default=None, help="Path for the structured final report JSON")
    parser.add_argument("--attempt-history-limit", type=int, default=None, help="Maximum persisted attempt/decision records")
    parser.add_argument("--no-loop-guard", action="store_true", help="Disable monitored-agent loop protection")
    parser.add_argument("--loop-repeat-limit", type=int, default=None, help="Repeated expensive command episodes allowed without progress")
    parser.add_argument("--queued-attempt-seconds", type=float, default=None, help="Seconds before a visibly queued message requires attention")
    parser.add_argument("--allow-history-rewrite", action="store_true", default=None, help="Allow monitored Git history-rewrite commands")
    parser.add_argument("--no-web-ui", action="store_true", help="Disable the local live web command center")
    parser.add_argument("--web-port", type=int, default=None, help="Preferred localhost port for the live web command center")
    parser.add_argument("--no-web-open", action="store_true", help="Start the web command center without opening a browser")
    parser.add_argument("--once", action="store_true", default=False, help="Inspect status once and exit")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Simulate actions without sending keystrokes")


def config_from_args(args: argparse.Namespace) -> MonitorConfig:
    """Build MonitorConfig merging defaults, discovered config file, and CLI flags."""
    project_dir = getattr(args, "project_dir", None) or "."
    file_cfg: dict[str, Any] = {}
    if getattr(args, "config", None):
        file_cfg = load_config_file(args.config)
    elif project_dir:
        discovered = discover_config_file(project_dir)
        if discovered:
            file_cfg = load_config_file(discovered)

    continue_text = getattr(args, "continue_text", None) if getattr(args, "continue_text", None) is not None else file_cfg.get("continue_text", "")
    if getattr(args, "continue_file", None):
        continue_path = Path(args.continue_file).resolve()
        if continue_path.is_file():
            continue_text = continue_path.read_text(encoding="utf-8").strip()

    is_supervise = bool(getattr(args, "supervise", False) or getattr(args, "command", "") == "supervise" or file_cfg.get("supervise", False))
    auto_allow = bool(getattr(args, "auto_allow_permissions", None) if getattr(args, "auto_allow_permissions", None) is not None else (is_supervise or file_cfg.get("auto_allow_permissions", False)))
    smart_nudges = not getattr(args, "no_smart_nudges", False) and bool(is_supervise or file_cfg.get("smart_nudges", True))
    auto_switch = not getattr(args, "no_mode_switch", False) and bool(is_supervise or file_cfg.get("auto_switch_modes", True))
    completion_check = not getattr(args, "no_completion_check", False) and bool(is_supervise or file_cfg.get("completion_check", True))

    process = getattr(args, "process", None) or file_cfg.get("process", "opencode")
    profile = getattr(args, "profile", None) or file_cfg.get("profile", process)

    def _val(arg_val: Any, cfg_key: str, default_val: Any) -> Any:
        if arg_val is not None:
            return arg_val
        return file_cfg.get(cfg_key, default_val)

    def _string_values(value: Any, default: list[str] | tuple[str, ...] = ()) -> list[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value]
        return list(default)

    cli_unsafe = getattr(args, "unsafe_phrases", None) or []
    file_unsafe = _string_values(file_cfg.get("unsafe_phrases", list(UNSAFE_PHRASES)), UNSAFE_PHRASES)
    merged_unsafe = list(dict.fromkeys([*file_unsafe, *cli_unsafe]))
    cli_prohibitions = getattr(args, "prohibitions", None) or []
    file_prohibitions = _string_values(file_cfg.get("prohibitions", []))
    merged_prohibitions = list(dict.fromkeys([*file_prohibitions, *cli_prohibitions]))

    return MonitorConfig(
        process=process,
        profile=profile,
        title=_val(getattr(args, "title", None), "title", None),
        continue_text=continue_text,
        continue_file=getattr(args, "continue_file", None),
        poll_seconds=float(_val(getattr(args, "poll_seconds", None), "poll_seconds", 3.0)),
        idle_seconds=float(_val(getattr(args, "idle_seconds", None), "idle_seconds", 15.0)),
        cooldown_seconds=float(_val(getattr(args, "cooldown_seconds", None), "cooldown_seconds", 20.0)),
        gone_seconds=float(_val(getattr(args, "gone_seconds", None), "gone_seconds", 25.0)),
        max_sends=int(_val(getattr(args, "max_sends", None), "max_sends", 100)),
        auto_allow_permissions=auto_allow,
        once=bool(getattr(args, "once", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        state_dir=str(_val(getattr(args, "state_dir", None), "state_dir", "/tmp/terminal-monitor")),
        backend=str(_val(getattr(args, "backend", None), "backend", "auto")),
        project_dir=str(project_dir),
        unsafe_phrases=merged_unsafe,
        custom_profiles=file_cfg.get("custom_profiles", {}),
        supervise=is_supervise,
        auto_switch_modes=auto_switch,
        smart_nudges=smart_nudges,
        completion_check=completion_check,
        status_json_path=getattr(args, "status_json_path", None) or file_cfg.get("status_json_path"),
        objective=str(_val(getattr(args, "objective", None), "objective", "")),
        prohibitions=merged_prohibitions,
        task_id=str(_val(getattr(args, "task_id", None), "task_id", "")),
        required_outcome=str(_val(getattr(args, "required_outcome", None), "required_outcome", "merged")),
        npm_publish_allowed=bool(_val(getattr(args, "allow_npm_publish", None), "npm_publish_allowed", False)),
        session_id=str(_val(getattr(args, "session_id", None), "session_id", "")),
        expected_branch=str(_val(getattr(args, "expected_branch", None), "expected_branch", "")),
        protected_branches=tuple(_string_values(_val(getattr(args, "protected_branches", None), "protected_branches", ["main", "master"]))),
        report_path=str(_val(getattr(args, "report_path", None), "report_path", "")) or None,
        attempt_history_limit=max(1, int(_val(getattr(args, "attempt_history_limit", None), "attempt_history_limit", 100))),
        loop_guard=not getattr(args, "no_loop_guard", False) and bool(file_cfg.get("loop_guard", True)),
        loop_repeat_limit=max(2, int(_val(getattr(args, "loop_repeat_limit", None), "loop_repeat_limit", 3))),
        queued_attempt_seconds=max(0.0, float(_val(getattr(args, "queued_attempt_seconds", None), "queued_attempt_seconds", 45.0))),
        allow_history_rewrite=bool(_val(getattr(args, "allow_history_rewrite", None), "allow_history_rewrite", False)),
        web_ui=not getattr(args, "no_web_ui", False) and bool(file_cfg.get("web_ui", True)),
        web_port=max(0, int(_val(getattr(args, "web_port", None), "web_port", 8765))),
        web_open_browser=not getattr(args, "no_web_open", False) and bool(file_cfg.get("web_open_browser", True)),
    )


def main() -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    # Handle init subcommand
    if args.command == "init":
        fmt = getattr(args, "format", "json")
        out_path = getattr(args, "output", None) or f".terminal-monitor.{fmt}"
        content = generate_starter_config(fmt)
        Path(out_path).write_text(content + "\n", encoding="utf-8")
        print(f"Created configuration template: {out_path}")
        return 0

    # Handle profiles subcommand
    if args.command == "profiles":
        print("Available Agent Profiles:")
        for name, desc in list_profiles().items():
            print(f"  • {name:<12} - {desc}")
        return 0

    config = config_from_args(args)
    if config.supervise:
        launch = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
        if "--state-dir" not in launch:
            launch.extend(["--state-dir", config.state_dir])
        if "--project-dir" not in launch and "-d" not in launch:
            launch.extend(["--project-dir", config.project_dir])
        for prohibition in config.prohibitions:
            if prohibition not in launch:
                launch.extend(["--prohibition", prohibition])
        config = replace(config, launch_command=tuple(launch))

    if args.command == "status":
        refresh = max(0.25, float(getattr(args, "interval", 2.0)))
        color = not bool(getattr(args, "no_color", False)) and sys.stdout.isatty()
        try:
            while True:
                snapshot = read_status_snapshot(config.state_dir, config.project_dir)
                if getattr(args, "status_json", False):
                    print(json.dumps(json_safe(snapshot), indent=2, sort_keys=True))
                else:
                    if getattr(args, "watch", False) and color:
                        print("\033[2J\033[H", end="")
                    print(render_status_dashboard(snapshot, color=color))
                if not getattr(args, "watch", False):
                    break
                time.sleep(refresh)
        except KeyboardInterrupt:
            return 130
        return 0

    if args.command == "stop":
        result = stop_monitor(config.state_dir, reason=str(getattr(args, "reason", "cli_stop")))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 2

    if args.command == "resume":
        result = resume_monitor(config.state_dir, project_dir=config.project_dir)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 2

    if args.command == "send":
        policy = PolicyEnvelope(config.objective, tuple(config.prohibitions))
        allowed, reason = policy.authorize_action(
            args.text,
            unsafe_phrases=config.unsafe_phrases,
            npm_publish_allowed=config.npm_publish_allowed,
        )
        if not allowed:
            print(f"POLICY_BLOCKED: {reason}", file=sys.stderr)
            return 3
        if config.dry_run:
            print(json.dumps({"dry_run": True, "action": "send", "payload": args.text, "policy": reason}, indent=2))
            return 0
        backend = get_backend(config.backend)
        ok, detail = backend.send(config.process, config.title, args.text)
        print(detail)
        return 0 if ok else 1

    if args.command == "interrupt-child":
        backend = get_backend(config.backend)
        roots = set(backend.get_pids(config.process))
        ok = interrupt_process_tree(roots, args.pid, parent_of=_parent_pid, children_of=_children_pids)
        print("INTERRUPTED_TREE" if ok else "REFUSED_NOT_VERIFIED_DESCENDANT")
        return 0 if ok else 2

    if args.command == "restart-agent":
        state = TaskState.load(Path(config.state_dir, "task-state.json"))
        try:
            command = build_restart_command(config, state, args.agent_command, args.continue_session)
        except StateFileError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if config.dry_run:
            print(json.dumps({"dry_run": True, "action": "restart-agent", "command": command}, indent=2))
            return 0
        state = persist_restart_event(Path(config.state_dir, "task-state.json"), state, command)
        process = subprocess.Popen(command, cwd=config.project_dir, start_new_session=True)
        print(f"RESTARTED pid={process.pid}")
        return 0

    if args.command == "merge-pr":
        state = TaskState.load(Path(config.state_dir, "task-state.json"))
        expected_head = args.head or str(state.pr.get("head", ""))
        if not expected_head:
            print("An expected full PR head SHA is required (--head or saved state).", file=sys.stderr)
            return 2
        result = merge_pull_request(config.project_dir, args.pr, expected_head, dry_run=config.dry_run)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 4

    if args.command == "verify-final-state":
        state = TaskState.load(Path(config.state_dir, "task-state.json"))
        evidence = collect_final_evidence(config.project_dir, state, args.pr)
        report = evaluate_final_state(evidence)
        report_path = config.report_path or str(Path(config.state_dir, "final-report.json"))
        write_final_report(report_path, state, evidence, report)
        print(json.dumps({"ok": report.ok, "checks": report.checks, "failures": report.failures, "evidence": evidence, "report_path": report_path}, indent=2))
        return 0 if report.ok else 4

    monitor = TerminalMonitor(config)

    if config.once:
        inspected = monitor.inspect()
        print(json.dumps(json_safe(inspected), indent=2, sort_keys=True))
        return 0 if inspected.get("ok") else 2

    return monitor.run()


if __name__ == "__main__":
    sys.exit(main())
