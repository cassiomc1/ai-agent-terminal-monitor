"""Terminal-history classification: state detection, todos, and question parsing."""
from __future__ import annotations

import re
from typing import Any

from .processes import ProcessActivity
from .profiles import BUILTIN_PROFILES, AgentProfile
from .safety import is_unsafe, redact_sensitive
from .state import SessionTracker

TODO_ITEM_PATTERN = re.compile(r"(?P<marker>\[(?:\s|x|X|•|·|✓|✔|~|-)\])\s*(?P<label>[^\[\r\n\│\┃]+)")
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


def find_full_task_titles(text: str) -> dict[str, str]:
    """Extract full task titles from plan outlines or numbered lists in history."""
    titles: dict[str, str] = {}
    pattern = re.compile(
        r"(?:^|\n)\s*(?:[-*•\d\.]+\s*)?(?:\b(?:Task|Tarefa)\s+(\d+)[:\s]+)(?P<title>[^\n\r\|┃│]{6,140})",
        re.IGNORECASE,
    )
    for m in pattern.finditer(str(text)):
        num = m.group(1)
        title_text = m.group("title").strip().rstrip("+-: ,(")
        if title_text and not title_text.endswith("-"):
            full = f"Task {num}: {title_text}"
            titles[f"task {num}"] = full
            titles[f"task{num}"] = full
    return titles


def reconcile_task_labels(items: list[dict[str, str]], history: str) -> list[dict[str, str]]:
    """Reconcile truncated TUI labels with full task descriptions found in history."""
    full_titles = find_full_task_titles(history)
    if not full_titles:
        return items
    reconciled = []
    for it in items:
        lbl = it["label"]
        m = re.search(r"\b(?:Task|Tarefa)\s+(\d+)\b", lbl, re.IGNORECASE)
        if m:
            key = f"task {m.group(1)}"
            if key in full_titles:
                lbl = full_titles[key]
        reconciled.append({"label": lbl, "state": it["state"]})
    return reconciled


def extract_todo_progress(history: str, *, session_history: str = "") -> dict[str, Any]:
    """Extract task progress, preferring a current explicit summary to TUI markers."""
    raw_items = []
    priority = {"pending": 0, "in_progress": 1, "completed": 2}
    for line in str(history).splitlines():
        for match in TODO_ITEM_PATTERN.finditer(line):
            raw_label = match.group("label")
            label = re.sub(r"\s+", " ", raw_label).strip(" │┃")
            if not label:
                continue
            marker = match.group("marker").lower()
            state = "completed" if marker in {"[x]", "[✓]", "[✔]"} else "in_progress" if marker in {"[•]", "[·]", "[~]", "[-]"} else "pending"
            raw_items.append({"label": label, "state": state})

    consolidated: dict[str, dict[str, str]] = {}
    for item in raw_items:
        lbl = item["label"]
        st = item["state"]
        norm_key = re.sub(r"[^a-z0-9]+", " ", lbl.lower()).strip()
        if not norm_key or len(norm_key) < 2:
            continue

        found_match = None
        for existing_key in list(consolidated.keys()):
            if norm_key == existing_key:
                found_match = existing_key
                break
            if norm_key.startswith(existing_key) or existing_key.startswith(norm_key):
                found_match = existing_key
                break

        if found_match:
            ex = consolidated[found_match]
            new_state = st if priority[st] > priority[ex["state"]] else ex["state"]
            new_label = lbl if len(lbl) > len(ex["label"]) else ex["label"]
            if len(norm_key) > len(found_match):
                del consolidated[found_match]
                consolidated[norm_key] = {"label": new_label, "state": new_state}
            else:
                consolidated[found_match] = {"label": new_label, "state": new_state}
        else:
            consolidated[norm_key] = {"label": lbl, "state": st}

    ordered = reconcile_task_labels(list(consolidated.values()), history)
    counts = {
        "total": len(ordered),
        "completed": sum(item["state"] == "completed" for item in ordered),
        "in_progress": sum(item["state"] == "in_progress" for item in ordered),
        "pending": sum(item["state"] == "pending" for item in ordered),
    }
    explicit_target = session_history if session_history else history
    explicit = _explicit_todo_completion(explicit_target, counts["total"])
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

def extract_options(history: str, profile: AgentProfile | None = None) -> list[tuple[str, bool]]:
    """Extract selectable options and their recommendation status from history."""
    prof = profile or BUILTIN_PROFILES["opencode"]
    tail = "\n".join(history.splitlines()[-60:])
    return prof.extract_options(tail)

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
