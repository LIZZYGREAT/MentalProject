import asyncio
import json
import uuid

import pytest

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.services.mutation_intent_verifier import (
    MutationIntentDecision,
    MutationIntentVerificationError,
    MutationIntentVerifier,
)


EMPTY_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
EVENT_SCHEMA = {
    "type": "object",
    "properties": {"event_id": {"type": "string", "minLength": 1}},
    "required": ["event_id"],
    "additionalProperties": False,
}


class StaticVerifier:
    def __init__(self, decision=None, error=None):
        self.decision = decision
        self.error = error
        self.calls = []

    async def verify(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.decision


class RecordingRuns:
    def __init__(self):
        self.calls = []

    def tool_call(self, *args):
        self.calls.append(args)


def context(
    text="帮我执行这个操作",
    *,
    turn_effect_policy="verify_on_demand",
    calendar_mutation_policy="normal",
    source_kind="text",
):
    return AgentContext(
        participant_id=uuid.uuid4(),
        participant_code="P-SENSITIVE",
        open_id="open-sensitive",
        chat_id="chat-sensitive",
        message_id="message-sensitive",
        agent_run_id=uuid.uuid4(),
        calendar_mutation_policy=calendar_mutation_policy,
        turn_effect_policy=turn_effect_policy,
        user_request_text=text,
        source_kind=source_kind,
    )


def register(
    registry,
    name,
    handler,
    *,
    effect="read",
    authorization_requirement="none",
    schema=EMPTY_SCHEMA,
):
    registry.register(
        name,
        name,
        schema,
        handler,
        effect=effect,
        authorization_requirement=authorization_requirement,
    )


def test_read_compute_and_ui_effects_never_call_mutation_verifier():
    verifier = StaticVerifier(error=AssertionError("verifier must not run"))
    registry = ToolRegistry(mutation_verifier=verifier)
    calls = []
    for name, effect in (
        ("read_tool", "read"),
        ("compute_tool", "compute"),
        ("ui_tool", "ui_effect"),
    ):
        register(
            registry,
            name,
            lambda _ctx, _args, current=name: calls.append(current)
            or {"ok": True},
            effect=effect,
        )

    async def scenario():
        return [await registry.execute(context(), name, {}) for name in registry.names]

    results = asyncio.run(scenario())
    assert all(result.status == "succeeded" for result in results)
    assert calls == ["read_tool", "compute_tool", "ui_tool"]
    assert verifier.calls == []


def test_schema_validation_precedes_mutation_authorization():
    verifier = StaticVerifier(
        MutationIntentDecision("allow", "direct_action", "direct_request")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    register(
        registry,
        "calendar_update_event",
        lambda _ctx, _args: {"ok": True},
        effect="external_write",
        authorization_requirement="direct_request",
        schema=EVENT_SCHEMA,
    )

    result = asyncio.run(registry.execute(context(), "calendar_update_event", []))
    missing = asyncio.run(registry.execute(context(), "calendar_update_event", {}))

    assert result.status == "invalid_arguments"
    assert missing.status == "invalid_arguments"
    assert verifier.calls == []


@pytest.mark.parametrize(
    ("text", "reason_code"),
    (
        ("你能添加日程吗？", "capability_question"),
        ("已经添加了吗？", "status_query"),
        ("如果改到三点会怎样？", "hypothetical"),
    ),
)
def test_verifier_denial_prevents_state_change(text, reason_code):
    verifier = StaticVerifier(
        MutationIntentDecision("deny", reason_code, reason_code)
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []
    register(
        registry,
        "care_update_preferences",
        lambda _ctx, args: handled.append(args) or {"ok": True},
        effect="internal_write",
        authorization_requirement="direct_request",
    )

    result = asyncio.run(
        registry.execute(context(text), "care_update_preferences", {})
    )

    assert result.status == "tool_effect_not_authorized"
    assert result.result == {
        "ok": False,
        "error": "tool_effect_not_authorized",
        "reason_code": reason_code,
    }
    assert handled == []


def test_ambiguous_mutation_returns_clarification_without_executing():
    verifier = StaticVerifier(
        MutationIntentDecision(
            "needs_clarification", "ambiguous", "ambiguous_target"
        )
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []
    register(
        registry,
        "calendar_update_event",
        lambda _ctx, args: handled.append(args) or {"ok": True},
        effect="external_write",
        authorization_requirement="direct_request",
        schema=EVENT_SCHEMA,
    )

    result = asyncio.run(
        registry.execute(
            context("把那个组会改一下"),
            "calendar_update_event",
            {"event_id": "provider-event"},
        )
    )

    assert result.status == "mutation_needs_clarification"
    assert result.result["reason_code"] == "ambiguous_target"
    assert handled == []


def test_direct_request_allow_executes_internal_and_external_writes():
    verifier = StaticVerifier(
        MutationIntentDecision("allow", "direct_action", "direct_request")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []
    register(
        registry,
        "care_record_checkin",
        lambda _ctx, args: handled.append(("care", args)) or {"ok": True},
        effect="internal_write",
        authorization_requirement="direct_request",
    )
    register(
        registry,
        "calendar_update_event",
        lambda _ctx, args: handled.append(("calendar", args)) or {"ok": True},
        effect="external_write",
        authorization_requirement="direct_request",
        schema=EVENT_SCHEMA,
    )

    care = asyncio.run(
        registry.execute(context("帮我记录一下"), "care_record_checkin", {})
    )
    calendar = asyncio.run(
        registry.execute(
            context("帮我把组会改到三点"),
            "calendar_update_event",
            {"event_id": "exact-event"},
        )
    )

    assert care.status == "succeeded"
    assert calendar.status == "succeeded"
    assert handled == [
        ("care", {}),
        ("calendar", {"event_id": "exact-event"}),
    ]
    assert len(verifier.calls) == 2


def test_destructive_capability_question_never_executes_delete():
    verifier = StaticVerifier(
        MutationIntentDecision("deny", "capability_question", "capability_question")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    deleted = []
    register(
        registry,
        "calendar_delete_event",
        lambda _ctx, args: deleted.append(args) or {"ok": True},
        effect="destructive_external_write",
        authorization_requirement="explicit_destructive_request",
        schema=EVENT_SCHEMA,
    )

    result = asyncio.run(
        registry.execute(
            context("这个活动能删吗？"),
            "calendar_delete_event",
            {"event_id": "exact-event"},
        )
    )

    assert result.status == "tool_effect_not_authorized"
    assert result.result["reason_code"] == "capability_question"
    assert deleted == []


def test_verifier_failure_fails_closed_but_does_not_affect_reads():
    verifier = StaticVerifier(error=TimeoutError("provider timeout"))
    registry = ToolRegistry(mutation_verifier=verifier)
    writes = []
    register(
        registry,
        "care_record_checkin",
        lambda _ctx, args: writes.append(args) or {"ok": True},
        effect="internal_write",
        authorization_requirement="direct_request",
    )
    register(
        registry,
        "care_get_recent_state",
        lambda _ctx, _args: {"ok": True},
    )

    write = asyncio.run(registry.execute(context(), "care_record_checkin", {}))
    read = asyncio.run(registry.execute(context(), "care_get_recent_state", {}))

    assert write.status == "authorization_unavailable"
    assert write.result["error"] == "mutation_authorization_unavailable"
    assert writes == []
    assert read.status == "succeeded"
    assert len(verifier.calls) == 1


def test_delete_uses_backend_verification_and_never_sends_event_id_or_identity():
    verifier = StaticVerifier(
        MutationIntentDecision(
            "allow", "destructive_action", "explicit_destructive_request"
        )
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    deleted = []
    register(
        registry,
        "calendar_delete_event",
        lambda _ctx, args: deleted.append(args["event_id"]) or {"ok": True},
        effect="destructive_external_write",
        authorization_requirement="explicit_destructive_request",
        schema=EVENT_SCHEMA,
    )

    result = asyncio.run(
        registry.execute(
            context("把这个活动删掉"),
            "calendar_delete_event",
            {"event_id": "provider-event-secret"},
        )
    )

    assert result.status == "succeeded"
    assert deleted == ["provider-event-secret"]
    proposal = verifier.calls[0]
    serialized = json.dumps(proposal, ensure_ascii=False)
    assert proposal["proposal_summary"]["exact_target_supplied"] is True
    assert "event_id" not in serialized
    assert "provider-event-secret" not in serialized
    assert "open-sensitive" not in serialized
    assert "chat-sensitive" not in serialized
    assert "P-SENSITIVE" not in serialized


def test_generic_image_evidence_does_not_bypass_semantic_authorization():
    verifier = StaticVerifier(
        MutationIntentDecision("deny", "capability_question", "capability_question")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []
    register(
        registry,
        "calendar_create_event",
        lambda _ctx, args: handled.append(args) or {"ok": True},
        effect="external_write",
        authorization_requirement="direct_request",
    )

    result = asyncio.run(
        registry.execute(
            context("帮我看看", source_kind="generic_image"),
            "calendar_create_event",
            {},
        )
    )

    assert result.status == "tool_effect_not_authorized"
    assert handled == []
    assert verifier.calls[0]["source_kind"] == "generic_image"
    assert "image" not in verifier.calls[0]["proposal_summary"]


def test_course_schedule_strict_context_blocks_writes_before_verifier():
    verifier = StaticVerifier(error=AssertionError("verifier must not run"))
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []
    register(
        registry,
        "calendar_create_event",
        lambda _ctx, args: handled.append(args) or {"ok": True},
        effect="external_write",
        authorization_requirement="direct_request",
    )

    result = asyncio.run(
        registry.execute(
            context(
                "帮我看看",
                turn_effect_policy="read_compute_only",
                calendar_mutation_policy="course_schedule_strict_only",
                source_kind="course_schedule_strict",
            ),
            "calendar_create_event",
            {},
        )
    )

    assert result.status == "tool_effect_not_authorized"
    assert result.result["reason_code"] == "read_compute_only"
    assert verifier.calls == []
    assert handled == []


def test_deterministic_backend_actions_cannot_be_replayed_as_agent_tools():
    verifier = StaticVerifier(error=AssertionError("verifier must not run"))
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []
    register(
        registry,
        "care_record_checkin",
        lambda _ctx, args: handled.append(args) or {"ok": True},
        effect="internal_write",
        authorization_requirement="direct_request",
    )

    result = asyncio.run(
        registry.execute(
            context(
                "trusted card event",
                turn_effect_policy="deterministic_backend_action",
                source_kind="card_action",
            ),
            "care_record_checkin",
            {},
        )
    )

    assert result.status == "tool_effect_not_authorized"
    assert result.result["reason_code"] == (
        "deterministic_action_outside_agent_registry"
    )
    assert verifier.calls == []
    assert handled == []


def test_calendar_operation_gate_remains_after_verifier_allow():
    verifier = StaticVerifier(
        MutationIntentDecision("allow", "direct_action", "direct_request")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []
    register(
        registry,
        "calendar_update_event",
        lambda _ctx, args: handled.append(args) or {"ok": True},
        effect="external_write",
        authorization_requirement="direct_request",
        schema=EVENT_SCHEMA,
    )

    result = asyncio.run(
        registry.execute(
            context(
                "把组会改到三点",
                calendar_mutation_policy="calendar_create_only",
            ),
            "calendar_update_event",
            {"event_id": "event"},
        )
    )

    assert len(verifier.calls) == 1
    assert result.status == "calendar_mutation_not_authorized"
    assert result.result["reason_code"] == "calendar_operation_not_allowed"
    assert handled == []


def test_tool_audit_contains_effect_authorization_reason_and_safe_summaries():
    runs = RecordingRuns()
    verifier = StaticVerifier(
        MutationIntentDecision("deny", "status_query", "status_query")
    )
    registry = ToolRegistry(runs, mutation_verifier=verifier)
    register(
        registry,
        "care_update_preferences",
        lambda _ctx, _args: {"ok": True},
        effect="internal_write",
        authorization_requirement="direct_request",
    )

    asyncio.run(
        registry.execute(
            context("已经改了吗？"), "care_update_preferences", {}
        )
    )

    _, tool_name, arguments_audit, result_audit, status = runs.calls[0]
    assert tool_name == "care_update_preferences"
    assert status == "tool_effect_not_authorized"
    for audit in (arguments_audit, result_audit):
        assert audit["tool"] == tool_name
        assert audit["effect"] == "internal_write"
        assert audit["authorization_requirement"] == "direct_request"
        assert audit["authorization_decision"] == "deny"
        assert audit["reason_code"] == "status_query"
        assert audit["status"] == "tool_effect_not_authorized"


class RawClient:
    def __init__(self, value):
        self.value = value

    def infer(self, _payload):
        return self.value


class CapturingClient(RawClient):
    def __init__(self, value):
        super().__init__(value)
        self.payloads = []

    def infer(self, payload):
        self.payloads.append(payload)
        return self.value


def test_verifier_redacts_secrets_and_identifiers_from_provider_payload():
    client = CapturingClient(
        {
            "decision": "deny",
            "intent": "capability_question",
            "reason_code": "capability_question",
        }
    )
    verifier = MutationIntentVerifier(client)

    asyncio.run(
        verifier.verify(
            user_request_text="能改吗 api_key=top-secret 学号=20260001",
            tool_name="calendar_update_event",
            tool_effect="external_write",
            authorization_requirement="direct_request",
            proposal_summary={
                "event_id": "provider-event-secret",
                "description": "access_token=provider-token",
            },
            source_kind="text",
        )
    )

    serialized = json.dumps(client.payloads[0], ensure_ascii=False)
    assert "top-secret" not in serialized
    assert "20260001" not in serialized
    assert "provider-event-secret" not in serialized
    assert "provider-token" not in serialized


def test_verifier_rejects_invalid_or_mismatched_provider_output():
    invalid = MutationIntentVerifier(
        RawClient(
            {
                "decision": "allow",
                "intent": "status_query",
                "reason_code": "status_query",
            }
        )
    )
    mismatched = MutationIntentVerifier(
        RawClient(
            {
                "decision": "allow",
                "intent": "direct_action",
                "reason_code": "direct_request",
            }
        )
    )
    kwargs = {
        "user_request_text": "删除这个活动",
        "tool_name": "calendar_delete_event",
        "tool_effect": "destructive_external_write",
        "authorization_requirement": "explicit_destructive_request",
        "proposal_summary": {"exact_target_supplied": True},
        "source_kind": "text",
    }

    with pytest.raises(MutationIntentVerificationError):
        asyncio.run(invalid.verify(**kwargs))
    with pytest.raises(MutationIntentVerificationError):
        asyncio.run(mismatched.verify(**kwargs))
