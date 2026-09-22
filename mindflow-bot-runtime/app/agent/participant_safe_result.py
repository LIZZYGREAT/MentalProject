"""Participant-safe presentation of backend tool results."""

from __future__ import annotations

from typing import Any, Mapping

from app.agent.context import AgentContext


_PUBLIC_KIND_BY_ERROR = {
    "invalid_arguments": "invalid_request",
    "tool_exception": "tool_failed",
    "tool_not_authorized": "not_authorized",
    "tool_effect_not_authorized": "not_authorized",
    "calendar_mutation_not_authorized": "not_authorized",
    "mutation_authorization_unavailable": "tool_unavailable",
    "authorization_unavailable": "tool_unavailable",
    "mutation_needs_clarification": "clarification_required",
    "public_url_not_readable": "public_url_not_readable",
    "repeated_non_retryable_tool_call": "not_retryable",
}

_PUBLIC_REASON_BY_KIND = {
    "invalid_request": "请求信息不完整，请检查后再试。",
    "tool_failed": "这项操作暂时没有完成，请稍后再试。",
    "not_authorized": "这项操作当前不能执行。",
    "tool_unavailable": "这项操作暂时不可用，请稍后再试。",
    "clarification_required": "还需要补充一项信息。",
    "not_retryable": "这次请求没有完成，请补充或调整后再试。",
    "public_url_not_readable": "这个链接暂时不能直接读取。",
}


def participant_safe_result(
    ctx: AgentContext, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the result allowed in the Agent-facing participant channel.

    Diagnostic mode is assigned by the backend when constructing the context;
    this function never infers it from a participant code.
    """

    value = dict(result)
    if ctx.participant_diagnostics_enabled:
        return value
    if value.get("ok") is True:
        return value
    # Existing registry errors such as tool_exception already contain only a
    # stable, non-raw failure contract.  Only results explicitly carrying
    # backend diagnostic text/summary need the stricter presentation rewrite.
    if "diagnostic_summary" not in value and "reason_text" not in value:
        return value

    raw_error = str(value.get("error") or "")
    kind = str(
        value.get("kind")
        or _PUBLIC_KIND_BY_ERROR.get(raw_error, "tool_failed")
    )
    public: dict[str, Any] = {
        "ok": False,
        "kind": kind,
        "error": kind,
        "public_reason": str(
            value.get("public_reason")
            or value.get("public_guidance")
            or _PUBLIC_REASON_BY_KIND.get(kind, _PUBLIC_REASON_BY_KIND["tool_failed"])
        ),
    }
    for key in (
        "public_guidance",
        "clarification",
        "staged",
        "persisted",
        "verified",
        "retryable",
        "do_not_retry",
    ):
        if key in value:
            public[key] = value[key]
    return public
