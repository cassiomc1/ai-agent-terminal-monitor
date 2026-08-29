"""GitHub/PR lifecycle: check classification, merge gate, and final verification."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .backends import run_command
from .config import MonitorConfig
from .gitinfo import GitStatus
from .state import StateFileError, TaskState, _atomic_json_write, now_iso
from .types import CheckClassification, MergeGateResult

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
def classify_check_result(check: dict[str, Any]) -> CheckClassification:
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
    result: CheckClassification = {
        "category": category,  # type: ignore[typeddict-item]
        "retryable": category in {"cancelled-infra", "failed-external"},
        "conclusion": raw,
        "evidence": evidence,
        "name": str(check.get("name") or check.get("context") or ""),
    }
    return result
class PullRequestStateMachine:
    """Map GitHub PR/check snapshots to an actionable supervision stage."""

    KNOWN_STAGES: frozenset[str] = frozenset(
        {
            "TASK_RECEIVED",
            "EXECUTING",
            "VERIFYING",
            "PR_CREATED",
            "CI_CHECKS",
            "CI_PENDING",
            "CI_GREEN",
            "CI_RETRY_REQUIRED",
            "FIX_REQUIRED",
            "POST_MERGE_VERIFY",
            "MERGED",
        }
    )

    def __init__(self) -> None:
        self._stage = "TASK_RECEIVED"
        self.seen_pr_number: int | None = None

    @property
    def stage(self) -> str:
        """Current stage; assign via :meth:`restore` so invariants stay consistent."""
        return self._stage

    def restore(self, stage: str, pr_number: int | None = None) -> None:
        """Restore a persisted stage, validating it and syncing the seen PR number.

        Restoring through this method (instead of poking ``stage`` directly)
        keeps ``seen_pr_number`` consistent with the restored stage so the
        next ``advance`` cannot misfire the PR_CREATED transition.
        """
        normalized = str(stage or "").strip().upper()
        if normalized and normalized not in self.KNOWN_STAGES:
            raise ValueError(f"Unknown PR stage: {stage!r}")
        self._stage = normalized or "TASK_RECEIVED"
        if pr_number is not None:
            self.seen_pr_number = int(pr_number)

    def advance(self, pr: dict[str, Any] | None) -> str:
        if not pr or not pr.get("number"):
            self._stage = "TASK_RECEIVED"
            return self._stage
        number = int(pr["number"])
        if self.seen_pr_number != number and self._stage == "TASK_RECEIVED":
            self.seen_pr_number = number
            self._stage = "PR_CREATED"
            return self._stage
        self.seen_pr_number = number
        checks = list(pr.get("checks") or [])
        classifications = [classify_check_result(check) for check in checks]
        conclusions = {item["category"] for item in classifications}
        if str(pr.get("state", "")).upper() == "MERGED":
            self._stage = "POST_MERGE_VERIFY"
        elif "failed" in conclusions:
            self._stage = "FIX_REQUIRED"
        elif conclusions & {"cancelled-infra", "failed-external"}:
            self._stage = "CI_RETRY_REQUIRED"
        elif checks and conclusions <= {"passed"}:
            self._stage = "CI_GREEN"
        else:
            self._stage = "CI_PENDING"
        return self._stage


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


def verify_merge_gate(project_dir: str, pr_number: int, expected_head: str) -> MergeGateResult:
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


def merge_pull_request(project_dir: str, pr_number: int, expected_head: str, *, dry_run: bool = False) -> MergeGateResult:
    """Merge only after the exact-head gate; dry-run never contacts GitHub."""
    if dry_run:
        return {"ok": True, "dry_run": True, "reason": "would_merge", "pr": pr_number, "head": expected_head}  # type: ignore[typeddict-item]
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
        result = subprocess.run(
            command,
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1.0, timeout_seconds),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False
