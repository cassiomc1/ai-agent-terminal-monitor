"""Descendant process inspection, interruption, and agent loop guarding."""
from __future__ import annotations

import contextlib
import os
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .backends import run_command
from .safety import redact_sensitive


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
    (
        "full-test-suite",
        r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test(?:[:\w.-]*)?\b|\b(?:node|bun|deno)\s+(?:\./)?scripts/run-tests\.js\b|\bpython\d*\s+-m\s+(?:unittest|pytest)\b|\bpytest(?:\s|$)|\bcargo\s+test(?:\s|$)|\bgo\s+test(?:\s|$)",
    ),
    ("ci-watch", r"\bgh\s+(?:pr\s+checks|run\s+watch)\b"),
    ("build", r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?build\b|\bcargo\s+build\b"),
)

GIT_HISTORY_REWRITE_PATTERNS: tuple[str, ...] = (
    r"\bgit\s+filter-branch\b",
    r"\bgit\s+rebase\b(?!\s+--(?:abort|continue|skip)\b)",
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
    sig: int = signal.SIGINT,
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
    signalled = False
    failed = False
    for pid in ordered:
        try:
            signaler(pid, sig)
            signalled = True
        except ProcessLookupError:
            continue
        except OSError:
            failed = True
    return bool(ordered) and (signalled or not failed) and not failed


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


def process_is_running(pid: int | str | None) -> bool:
    """Treat exited and zombie descendants as stopped for recovery purposes."""
    value = int(pid or 0) if str(pid or "0").lstrip("-").isdigit() else 0
    if value <= 0 or not pid_is_alive(value):
        return False
    code, output, _ = run_command(["ps", "-p", str(value), "-o", "stat="])
    return not (code == 0 and output.strip().upper().startswith("Z"))
