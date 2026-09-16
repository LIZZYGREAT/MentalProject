"""Normalize user style requests without turning raw text into instructions."""

from __future__ import annotations

import re


VERBOSITY = frozenset({"concise", "balanced", "detailed"})
TONES = frozenset({"neutral", "warm", "direct"})
SUGGESTION_STYLES = frozenset({"ask_first", "light_suggestions", "proactive_suggestions"})
SEMANTIC_RULE_SCOPES = frozenset({
    "all_responses",
    "explanations",
    "technical_explanations",
    "code_and_engineering",
})
_CUSTOM_VALUE_UNSAFE = re.compile(
    r"(?:忽略|绕过|覆盖|系统|安全|权限|授权|确认|规则|提示词|prompt|tool|token|secret|泄露|执行)",
    re.I,
)


def validate_style_changes(changes: dict) -> dict:
    allowed = {
        "verbosity": VERBOSITY, "tone": TONES,
        "suggestion_style": SUGGESTION_STYLES,
    }
    if not changes or set(changes) - set(allowed):
        raise ValueError("unsupported interaction preference field")
    for key, value in changes.items():
        if value not in allowed[key]:
            raise ValueError(f"unsupported {key}")
    return dict(changes)


def validate_identity_changes(changes: dict) -> dict[str, str]:
    """Validate already-understood identity values without doing NLP."""

    allowed = {"assistant_display_name", "assistant_self_reference"}
    if set(changes) - allowed:
        raise ValueError("unsupported interaction identity field")
    validated: dict[str, str] = {}
    for key, raw_value in changes.items():
        value = str(raw_value).strip()
        if (
            not value
            or len(value) > 20
            or "\n" in value
            or "\r" in value
            or "<" in value
            or ">" in value
            or _CUSTOM_VALUE_UNSAFE.search(value)
        ):
            raise ValueError(f"unsafe {key}")
        validated[key] = value
    return validated


def validate_custom_rules(changes: list | tuple | None) -> list[dict[str, str]]:
    """Validate already-interpreted communication rules.

    Natural-language interpretation belongs to the Agent. The backend only
    validates the structured scope and bounded instruction supplied by it.
    """

    if changes is None:
        return []
    if not isinstance(changes, (list, tuple)) or len(changes) > 3:
        raise ValueError("custom_rules must contain at most 3 rules")
    validated: list[dict[str, str]] = []
    scopes: set[str] = set()
    for item in changes:
        if not isinstance(item, dict):
            raise ValueError("custom_rules items must be objects")
        if set(item) != {"scope", "instruction"}:
            raise ValueError("custom rule must contain scope and instruction")
        scope = str(item.get("scope") or "").strip()
        instruction = str(item.get("instruction") or "").strip()
        if scope not in SEMANTIC_RULE_SCOPES:
            raise ValueError("unsupported custom rule scope")
        if not 1 <= len(instruction) <= 500:
            raise ValueError("custom rule instruction must be 1-500 characters")
        if "\n" in instruction or "\r" in instruction:
            raise ValueError("custom rule instruction cannot contain line breaks")
        if scope in scopes:
            raise ValueError("custom_rules cannot contain duplicate scopes")
        scopes.add(scope)
        validated.append({"scope": scope, "instruction": instruction})
    return validated
