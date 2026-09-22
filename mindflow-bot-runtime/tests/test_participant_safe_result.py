import asyncio
import json
import uuid

from app.agent.context import AgentContext
from app.agent.participant_safe_result import participant_safe_result
from app.agent.tool_registry import ToolRegistry
from app.repositories_care import CarePreferenceClarificationRequired


def _context(*, diagnostics: bool = False) -> AgentContext:
    return AgentContext(
        uuid.uuid4(), "P002", "open", "chat", "message", uuid.uuid4(),
        participant_diagnostics_enabled=diagnostics,
    )


def test_care_clarification_has_public_guidance_and_bounded_diagnostics():
    result = CarePreferenceClarificationRequired(
        missing_fields=("quiet_hours_end",),
        resolved_changes={
            "morning_brief_local_time": "08:30",
            "quiet_hours_start": "23:00",
        },
    ).as_tool_result()

    assert result["kind"] == "clarification_required"
    assert result["public_guidance"] == "晚上 23:00 开始免打扰，到几点恢复提醒？"
    assert result["diagnostic_summary"]["missing_fields"] == ["quiet_hours_end"]
    assert "exception" not in json.dumps(result, ensure_ascii=False)

    public = participant_safe_result(_context(), result)
    assert public == {
        "ok": False,
        "kind": "clarification_required",
        "error": "clarification_required",
        "public_reason": "晚上 23:00 开始免打扰，到几点恢复提醒？",
        "public_guidance": "晚上 23:00 开始免打扰，到几点恢复提醒？",
        "clarification": {
            "topic": "quiet_hours",
            "known": {"start": "23:00", "morning_brief": "08:30"},
            "need": "end_time",
        },
        "staged": False,
        "persisted": False,
        "do_not_retry": True,
    }
    assert "quiet_hours_end" not in json.dumps(public, ensure_ascii=False)
    assert "reason_code" not in json.dumps(public, ensure_ascii=False)

    diagnostic = participant_safe_result(_context(diagnostics=True), result)
    assert diagnostic["diagnostic_summary"]["reason_code"] == (
        "care_preference_incomplete"
    )


def test_tool_registry_keeps_diagnostics_out_of_normal_agent_result():
    registry = ToolRegistry()

    def handler(_ctx, _args):
        raise CarePreferenceClarificationRequired(
            missing_fields=("quiet_hours_end",),
            resolved_changes={"quiet_hours_start": "23:00"},
        )

    registry.register(
        "care_update_preferences",
        "care",
        {"type": "object", "properties": {}, "additionalProperties": False},
        handler,
        effect="proposal_stage",
        authorization_requirement="none",
    )

    public = asyncio.run(
        registry.execute(_context(), "care_update_preferences", {})
    )
    assert public.status == "needs_clarification"
    assert public.result["kind"] == "clarification_required"
    assert "reason_code" not in public.result
    assert "quiet_hours_end" not in json.dumps(public.result, ensure_ascii=False)

    diagnostic = asyncio.run(
        registry.execute(
            _context(diagnostics=True), "care_update_preferences", {}
        )
    )
    assert diagnostic.result["diagnostic_summary"]["reason_code"] == (
        "care_preference_incomplete"
    )


def test_generic_failed_results_have_public_copy_and_diagnostic_mode_is_backend_set():
    internal = {
        "ok": False,
        "error": "public_url_not_readable",
        "reason_code": "secret_query_not_allowed",
        "reason_text": "internal guard detail",
        "diagnostic_summary": {"reason_code": "secret_query_not_allowed"},
        "verified": False,
    }

    public = participant_safe_result(_context(), internal)
    assert public == {
        "ok": False,
        "kind": "public_url_not_readable",
        "error": "public_url_not_readable",
        "public_reason": "这个链接暂时不能直接读取。",
        "verified": False,
    }
    assert "secret_query_not_allowed" not in json.dumps(public, ensure_ascii=False)
    assert participant_safe_result(_context(diagnostics=True), internal) == internal
