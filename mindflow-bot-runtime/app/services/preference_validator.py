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
    if re.search(r"先听我说完|先听我说|先共情", text):
        candidates.append(("acknowledge_before_advice", True, "先回应感受再给建议"))
    if re.search(r"先问我.{0,8}(?:建议|办法)|想不想听建议", text):
        candidates.append(("ask_before_suggestion", True, "给建议前先询问"))
    elif re.search(r"(?:以后)?不用先问|直接给建议", text):
        candidates.append(("ask_before_suggestion", False, "无需先询问即可给建议"))
    suggestion_count = re.search(r"最多给我?([一二两三123])条建议", text)
    if suggestion_count:
        count = {"一": 1, "二": 2, "两": 2, "三": 3}.get(
            suggestion_count.group(1), int(suggestion_count.group(1))
            if suggestion_count.group(1).isdigit() else 1
        )
        candidates.append(("max_suggestions", count, f"每次最多{count}条建议"))
    elif re.search(r"别一次给我很多|不要一次给太多|少给.{0,4}办法", text):
        candidates.append(("max_suggestions", 1, "每次最多一条建议"))
    if re.search(r"别.{0,8}(?:主动跟进|主动问我)|不要.{0,8}(?:主动跟进|主动问我)", text):
        candidates.append(("allow_supportive_follow_up", False, "不主动支持性跟进"))
    elif re.search(r"可以.{0,6}(?:主动关心我|主动跟进|主动问我)", text):
        candidates.append(("allow_supportive_follow_up", True, "允许主动支持性跟进"))
    if re.search(r"实际一点|实用一点|可操作", text):
        candidates.append(("preferred_support_style", "practical", "偏好实用支持"))
    elif re.search(r"主要想让你听我说|以倾听为主", text):
        candidates.append(("preferred_support_style", "listening", "偏好倾听支持"))
    elif re.search(r"温柔一点|温和地支持", text):
        candidates.append(("preferred_support_style", "gentle", "偏好温和支持"))
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
