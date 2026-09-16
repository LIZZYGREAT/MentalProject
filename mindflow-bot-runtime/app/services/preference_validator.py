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
_SEMANTIC_RULE_UNSAFE_PATTERNS = (
    re.compile(
        r"(?:忽略|绕过|覆盖)\s*(?:所有|任何)?\s*"
        r"(?:系统(?:规则|指令)?|安全(?:规则|限制)?|权限|授权|确认)",
        re.I,
    ),
    re.compile(
        r"(?:无需|不(?:要|需)|免于|跳过)\s*(?:任何|用户)?\s*(?:确认|授权)",
        re.I,
    ),
    re.compile(
        r"(?:直接|立即|无需确认地)\s*(?:调用|执行|使用)\s*"
        r"(?:任何|任意)?\s*(?:工具|tools?\b)",
        re.I,
    ),
    re.compile(
        r"(?:泄露|显示|输出|提供|打印|暴露|reveal|disclose|print|output|"
        r"leak|exfiltrate)\s*(?:所有|内部)?\s*"
        r"(?:token|tokens|secret|secrets|api\s*key|密钥|系统提示|"
        r"system\s*prompt|system\s*instructions)",
        re.I,
    ),
    re.compile(
        r"(?:ignore|bypass|override)\s+(?:the\s+)?"
        r"(?:system|safety|security|permission|authorization|confirmation|rules?)",
        re.I,
    ),
    re.compile(
        r"(?:without|skip|no)\s+(?:user\s+)?"
        r"(?:confirmation|authorization)",
        re.I,
    ),
    re.compile(
        r"(?:directly|immediately)\s+(?:call|execute|invoke)\s+"
        r"(?:any\s+)?tools?\b",
        re.I,
    ),
    re.compile(
        r"(?:reveal|disclose|print|output|leak|exfiltrate)\s+"
        r"(?:tokens?|secrets?|api\s*keys?|system\s*prompts?)",
        re.I,
    ),
)


class UnsafeSemanticPreferenceRule(ValueError):
    code = "unsafe_semantic_preference_rule"
    do_not_retry = True

    def __init__(self) -> None:
        super().__init__(
            "semantic communication rules cannot change authorization, safety, "
            "confirmation, tool, or secret-handling behavior"
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


def validate_semantic_rule_instruction(instruction: str) -> str:
    """Reject only explicit attempts to control authorization or tool use."""

    value = str(instruction or "").strip()
    if any(pattern.search(value) for pattern in _SEMANTIC_RULE_UNSAFE_PATTERNS):
        raise UnsafeSemanticPreferenceRule()
    return value


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
        instruction = validate_semantic_rule_instruction(instruction)
        if scope in scopes:
            raise ValueError("custom_rules cannot contain duplicate scopes")
        scopes.add(scope)
        validated.append({"scope": scope, "instruction": instruction})
    return validated
