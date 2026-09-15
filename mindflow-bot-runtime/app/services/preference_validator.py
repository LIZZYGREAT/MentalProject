"""Normalize user style requests without turning raw text into instructions."""

from __future__ import annotations

import re


VERBOSITY = frozenset({"concise", "balanced", "detailed"})
TONES = frozenset({"neutral", "warm", "direct"})
SUGGESTION_STYLES = frozenset({"ask_first", "light_suggestions", "proactive_suggestions"})
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
