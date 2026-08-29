"""Safety rules, sensitive-data redaction, and the durable policy envelope."""
from __future__ import annotations

import re
from dataclasses import dataclass

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
def is_unsafe(value: str, unsafe_list: list[str] | tuple[str, ...] | None = None) -> bool:
    """Check if an option or command string matches known unsafe phrases."""
    phrases = unsafe_list if unsafe_list is not None else UNSAFE_PHRASES
    low = value.lower()
    return any(phrase.lower() in low for phrase in phrases)
