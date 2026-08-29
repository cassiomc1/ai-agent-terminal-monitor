"""Durable task state, attempt ledger, and atomic persistence primitives."""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    with contextlib.suppress(OSError):
        os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)


def _atomic_text_write(path: str | Path, text: str) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)

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
def now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def normalize_snapshot(history: str) -> str:
    """Clean and normalize history snapshot for state hashing."""
    text = re.sub(r"[ \t]+", " ", history)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-30:])
def append_log(path: str, message: str, *, max_bytes: int = 2_000_000) -> None:
    """Append a timestamped log line and retain a bounded one-file archive."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() and target.stat().st_size >= max(1024, int(max_bytes)):
            archive = target.with_name(f"{target.name}.1")
            os.replace(target, archive)
            with contextlib.suppress(OSError):
                os.chmod(archive, 0o600)
    except OSError:
        pass
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{now_iso()} {message}\n")
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
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
