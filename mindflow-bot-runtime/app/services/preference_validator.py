"""Normalize user style requests without turning raw text into instructions."""

from __future__ import annotations

import re


VERBOSITY = frozenset({"concise", "balanced", "detailed"})
TONES = frozenset({"neutral", "warm", "direct"})
SUGGESTION_STYLES = frozenset({"ask_first", "light_suggestions", "proactive_suggestions"})
_OVERRIDE = re.compile(r"(?:忽略|绕过|取消|覆盖).{0,12}(?:安全|系统|权限|授权|确认|规则)|(?:不用|无需).{0,6}(?:确认|授权)", re.I)


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


def normalize_rule(raw_text: str) -> dict:
    text = " ".join(str(raw_text).split())[:500]
    rejected = []
    if _OVERRIDE.search(text):
        rejected.append("authorization_or_safety_override")
    candidates = []
    if re.search(r"简短|简洁|少一点|短一点", text):
        candidates.append(("verbosity", "concise", "回答简短"))
    elif re.search(r"详细|展开|多解释", text):
        candidates.append(("verbosity", "detailed", "回答详细"))
    if re.search(r"直接一点|直说", text):
        candidates.append(("tone", "direct", "表达直接"))
    elif re.search(r"温和|温暖|柔和", text):
        candidates.append(("tone", "warm", "语气温和"))
    elif re.search(r"中性|客观", text):
        candidates.append(("tone", "neutral", "语气中性"))
    if re.search(r"先问我|建议前先问", text):
        candidates.append(("suggestion_style", "ask_first", "建议前先询问"))
    elif re.search(r"主动建议|多给建议", text):
        candidates.append(("suggestion_style", "proactive_suggestions", "主动提供建议"))
    elif re.search(r"少给建议|轻一点的建议", text):
        candidates.append(("suggestion_style", "light_suggestions", "只提供轻量建议"))
    if not candidates:
        if rejected:
            return {"accepted": [], "rejected": rejected}
        raise ValueError("interaction rule could not be normalized safely")
    return {
        "accepted": [
            {"category": category, "value": value, "safe_text": safe_text}
            for category, value, safe_text in candidates
        ],
        "rejected": rejected,
    }
