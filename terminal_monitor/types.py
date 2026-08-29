"""Shared structural types for the monitor's dict-shaped payloads.

The monitor deliberately stays dependency-free; these ``TypedDict`` definitions
give the main producer/consumer boundaries static names so key typos (e.g.
``"headRefOid"`` vs ``"head"``) can be caught by a type checker instead of
only failing at runtime.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

CheckCategory = Literal["passed", "failed", "failed-external", "cancelled-infra", "pending", "unknown"]


class TabResult(TypedDict, total=False):
    """Normalized terminal-tab snapshot produced by ``parse_tab_output``."""

    ok: bool
    error: str
    win: str
    tab: str
    title: str
    busy: bool
    wname: str
    hist: str


class CheckClassification(TypedDict):
    """Result of :func:`terminal_monitor.github.classify_check_result`."""

    category: CheckCategory
    retryable: bool
    conclusion: str
    evidence: str
    name: str


class MergeGateResult(TypedDict, total=False):
    """Result of the exact-head merge gate and merge attempt."""

    ok: bool
    reason: str
    expected_head: str
    actual_head: str
    head: str
    state: str
    detail: str
    checks: list[CheckClassification]
    pr: dict[str, Any]
    merged: bool
    dry_run: bool
    gate: MergeGateResult


class AttemptRecord(TypedDict, total=False):
    """One attempt-ledger record (queued/sent/accepted/...)."""

    attempt_id: str
    status: Literal["queued", "sent", "accepted", "completed", "ignored"]
    timestamp: str
    monotonic: float
    reason: str
    payload: str
    observed_state: str
    detail: str


class ProcessActivityView(TypedDict, total=False):
    """JSON-safe projection of :class:`terminal_monitor.processes.ProcessActivity`."""

    active: bool
    descendants: list[int]
    direct_descendants: list[int]
    commands: list[str]
    cpu_percent: float
    oldest_seconds: float
    git_changed: bool
    duplicate_commands: list[str]
    expensive_roots: list[int]
    test_progress: dict[str, Any] | None


__all__ = [
    "AttemptRecord",
    "CheckCategory",
    "CheckClassification",
    "MergeGateResult",
    "ProcessActivityView",
    "TabResult",
]
