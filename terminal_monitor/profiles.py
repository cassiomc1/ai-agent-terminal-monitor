"""Agent profiles describing how to detect and interact with AI agent CLIs."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from .safety import DEFAULT_PREFERRED_ANSWERS, UNSAFE_PHRASES
from .state import is_table_or_box_line, match_pattern


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
def clean_option(line: str) -> str:
    """Strip menu bullets, numbers, and recommended markers from an option string."""
    value = re.sub(r"^[\s│┃>*●○◉❯-]+", "", line).strip()
    value = re.sub(r"^\d+[.)\]]\s*", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s*\(recommended\)\s*", "", value, flags=re.IGNORECASE).strip()
