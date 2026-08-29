"""Git context engine: repository status cache, discovery, and smart nudges."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import run_command
from .safety import redact_sensitive
from .state import debug_swallow, json_safe, now_iso


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


def discover_agent_project_dir(pids: list[int] | tuple[int, ...] | set[int]) -> str | None:
    """Discover the working directory of a running agent process via lsof or /proc."""
    for pid in pids:
        try:
            # macOS / Unix lsof
            proc = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if line.startswith("n"):
                        candidate = line[1:].strip()
                        if candidate and os.path.isdir(candidate):
                            return candidate
            # Linux /proc/<pid>/cwd
            proc_cwd = Path(f"/proc/{pid}/cwd")
            if proc_cwd.exists():
                resolved = str(proc_cwd.resolve())
                if os.path.isdir(resolved):
                    return resolved
        except Exception as exc:
            debug_swallow("discover_agent_project_dir", exc)
            continue
    return None


def ensure_private_dir(path: str | Path) -> Path:
    """Create (or harden) a state directory so only the current user can use it.

    ``answer.txt`` is a command-injection channel and ``status.json`` /
    ``terminal-snapshot.txt`` are readable operational data, so state
    directories must never be group/world accessible.  The owner check fails
    closed against a pre-created attacker-owned directory in a shared /tmp.
    """
    target = Path(path)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o700)
    try:
        if target.stat().st_uid != os.getuid():
            raise PermissionError(f"State directory {target} is owned by another user; refusing to use it")
    except FileNotFoundError:
        pass
    return target


def resolve_project_state_dir(base_state_dir: str, project_dir: str) -> str:
    """Isolate monitor state and logs per project directory."""
    try:
        resolved_proj = str(Path(project_dir).resolve())
    except Exception:
        resolved_proj = str(project_dir)
    proj_hash = hashlib.sha256(resolved_proj.encode("utf-8")).hexdigest()[:10]
    proj_name = Path(resolved_proj).name or "project"
    scoped_dir = ensure_private_dir(Path(base_state_dir, f"{proj_name}-{proj_hash}"))
    return str(scoped_dir)


def send_desktop_notification(title: str, message: str) -> bool:
    """Send a native desktop notification on macOS or Linux without blocking."""
    try:
        clean_title = redact_sensitive(title).replace('"', '\\"')
        clean_msg = redact_sensitive(message).replace('"', '\\"')
        if sys.platform == "darwin":
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{clean_msg}" with title "{clean_title}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        elif shutil.which("notify-send"):
            subprocess.Popen(
                ["notify-send", clean_title, clean_msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except Exception as exc:
        debug_swallow("send_desktop_notification", exc)
    return False


def dispatch_webhook(webhook_url: str, event_type: str, payload: dict[str, Any]) -> bool:
    """Post an event payload to a configured webhook URL asynchronously in a daemon thread."""
    if not webhook_url:
        return False

    def _post() -> None:
        try:
            body = json.dumps({"event": event_type, "timestamp": now_iso(), "data": json_safe(payload)}).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "AI-Agent-Terminal-Monitor/2.0"},
            )
            with urllib.request.urlopen(req, timeout=4.0):
                pass
        except Exception as exc:
            debug_swallow("dispatch_webhook", exc)

    threading.Thread(target=_post, name="terminal-monitor-webhook", daemon=True).start()
    return True


def extract_test_progress(log_text: str) -> dict[str, Any] | None:
    """Extract real-time test execution counts from log outputs."""
    if not log_text:
        return None
    sum_match = re.search(r"Tests:\s*(?:(\d+)\s*passed)?(?:[,\s]*(\d+)\s*failed)?(?:[,\s]*(\d+)\s*total)?", log_text, re.IGNORECASE)
    if sum_match:
        p_str, f_str, t_str = sum_match.groups()
        passed = int(p_str or 0)
        failed = int(f_str or 0)
        total = int(t_str or (passed + failed))
    else:
        pass_matches = re.findall(r"(?:✔|✓|\bPASS\b)\s+", log_text)
        fail_matches = re.findall(r"(?:✖|✗|\bFAIL\b)\s+", log_text)
        if pass_matches or fail_matches:
            passed = len(pass_matches)
            failed = len(fail_matches)
            total = passed + failed
        else:
            return None

    if total > 0 or passed > 0 or failed > 0:
        pct = round((passed / max(1, total)) * 100, 1) if total else 0.0
        return {"passed": passed, "failed": failed, "total": total, "percent": pct}
    return None



GIT_STATUS_TTL_SECONDS = 30.0
# ``gh pr list`` is a network call; caching it independently keeps the hot
# poll loop from hitting the GitHub API every few seconds (rate limits).
OPEN_PRS_TTL_SECONDS = 60.0
_GIT_STATUS_CACHE: dict[str, tuple[float, GitStatus]] = {}
_OPEN_PRS_CACHE: dict[str, tuple[float, int]] = {}
# Thread-safety: the monitor loop and web-server threads may both refresh;
# dict assignment is atomic in CPython but check-then-set is not, and the
# duplicated refresh would double the git subprocess fan-out.
_GIT_STATUS_LOCK = threading.Lock()
_OPEN_PRS_LOCK = threading.Lock()


def get_git_status(repo_dir: str = ".", ttl_seconds: float = GIT_STATUS_TTL_SECONDS) -> GitStatus:
    """Cached wrapper around :func:`_get_local_git_fields` with a TTL per repository.

    Local ``git`` data uses ``ttl_seconds``; the network-backed open-PR count
    uses its own, longer TTL so supervision refreshes never rate-limit the
    GitHub API.
    """
    try:
        key = str(Path(repo_dir).resolve())
    except OSError:
        key = repo_dir
    now = time.monotonic()
    with _GIT_STATUS_LOCK:
        cached = _GIT_STATUS_CACHE.get(key)
        if cached is not None and now - cached[0] < ttl_seconds:
            return cached[1]
    fields = _get_local_git_fields(repo_dir)
    if fields is None:
        status = GitStatus(is_repo=False)
    else:
        branch, head, dirty, modified, untracked, modified_files, last_commit = fields
        open_prs = _get_open_pr_count(repo_dir)
        status = GitStatus(
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
            summary=f"branch={branch} dirty={dirty} mod={modified} untracked={untracked} prs={open_prs}",
        )
    with _GIT_STATUS_LOCK:
        _GIT_STATUS_CACHE[key] = (now, status)
    return status


def _get_open_pr_count(repo_dir: str) -> int:
    """Fetch the open-PR count with its own network TTL cache."""
    if not shutil.which("gh"):
        return 0
    try:
        key = str(Path(repo_dir).resolve())
    except OSError:
        key = repo_dir
    now = time.monotonic()
    with _OPEN_PRS_LOCK:
        cached = _OPEN_PRS_CACHE.get(key)
        if cached is not None and now - cached[0] < OPEN_PRS_TTL_SECONDS:
            return cached[1]
        gh_code, gh_out, _ = run_command(["gh", "pr", "list", "--state", "open", "--json", "number"], cwd=repo_dir)
        open_prs = 0
        if gh_code == 0:
            with contextlib.suppress(Exception):
                open_prs = len(json.loads(gh_out))
        _OPEN_PRS_CACHE[key] = (now, open_prs)
        return open_prs


def _get_local_git_fields(repo_dir: str) -> tuple[str, str, bool, int, int, tuple[str, ...], str] | None:
    """Inspect local git state safely without mutating the workspace.

    Returns ``None`` when the directory is not inside a git work tree.
    """
    try:
        code, out, _ = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
        if code != 0 or "true" not in out:
            return None

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
        return (branch, head, dirty, modified, untracked, modified_files, last_commit)
    except Exception as exc:
        debug_swallow("_get_local_git_fields", exc)
        return None


def _get_git_status_uncached(repo_dir: str = ".") -> GitStatus:
    """Uncached snapshot; kept for callers that must bypass both TTL caches."""
    try:
        code, out, _ = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_dir)
        if code != 0 or "true" not in out:
            return GitStatus(is_repo=False)
        fields = _get_local_git_fields(repo_dir)
        if fields is None:
            return GitStatus(is_repo=False)
        branch, head, dirty, modified, untracked, modified_files, last_commit = fields
        open_prs = _get_open_pr_count(repo_dir)
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
            summary=f"branch={branch} dirty={dirty} mod={modified} untracked={untracked} prs={open_prs}",
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
