import asyncio
import itertools
import json
import uuid

import pytest

import app.services.mutation_intent_verifier as mutation_intent_verifier_module
from app.agent.context import AgentContext, AuthorizationSemanticTurn
from app.agent.tool_registry import (
    AuthorizationContextResolutionError,
    ToolRegistry,
)
from app.services.mutation_intent_verifier import (
    MutationIntentDecision,
    MutationIntentVerificationError,
    MutationIntentVerifier,
)
from app.tools.care import CareTools


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
    semantic_turns=(),
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
        authorization_semantic_context=tuple(semantic_turns),
    )


def register(
    registry,
    name,
    handler,
    *,
    effect="read",
    authorization_requirement="none",
    schema=EMPTY_SCHEMA,
    authorization_context_resolver=None,
):
    registry.register(
        name,
        name,
        schema,
        handler,
        effect=effect,
        authorization_requirement=authorization_requirement,
        authorization_context_resolver=authorization_context_resolver,
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


@pytest.mark.parametrize(
    ("turn_effect_policy", "effect", "allowed"),
    (
        ("read_compute_only", "read", True),
        ("read_compute_only", "compute", True),
        ("read_compute_only", "ui_effect", False),
        ("read_compute_only", "internal_write", False),
        ("read_compute_only", "external_write", False),
        ("read_compute_only", "destructive_external_write", False),
        ("deterministic_backend_action", "read", False),
        ("deterministic_backend_action", "compute", False),
        ("deterministic_backend_action", "ui_effect", False),
        ("deterministic_backend_action", "internal_write", False),
        ("deterministic_backend_action", "external_write", False),
        (
            "deterministic_backend_action",
            "destructive_external_write",
            False,
        ),
    ),
)
def test_turn_effect_policy_matrix(turn_effect_policy, effect, allowed):
    verifier = StaticVerifier(error=AssertionError("verifier must not run"))
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []
    resolved = []
    authorization_requirement = (
        "explicit_destructive_request"
        if effect == "destructive_external_write"
        else "direct_request"
        if effect in {"internal_write", "external_write"}
        else "none"
    )

    def resolve_target(_ctx, _args):
        resolved.append(True)
        return {"target": {"summary": "must not resolve"}}

    register(
        registry,
        f"{effect}_tool",
        lambda _ctx, args: handled.append(args) or {"ok": True},
        effect=effect,
        authorization_requirement=authorization_requirement,
        authorization_context_resolver=(
            resolve_target
            if effect
            in {"internal_write", "external_write", "destructive_external_write"}
            else None
        ),
    )

    result = asyncio.run(
        registry.execute(
            context(turn_effect_policy=turn_effect_policy),
            f"{effect}_tool",
            {},
        )
    )

    assert result.status == (
        "succeeded" if allowed else "tool_effect_not_authorized"
    )
    assert handled == ([{}] if allowed else [])
    assert resolved == []
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

    async def resolve_target(_ctx, _args):
        return {
            "target": {
                "summary": "项目组会",
                "start_time": "2026-09-08T15:00:00+08:00",
                "end_time": "2026-09-08T16:00:00+08:00",
                "recurrence": "FREQ=WEEKLY",
                "provider_id": "must-not-leave-backend",
            },
            "calendar_id": "must-not-leave-backend",
        }

    register(
        registry,
        "calendar_delete_event",
        lambda _ctx, args: deleted.append(args["event_id"]) or {"ok": True},
        effect="destructive_external_write",
        authorization_requirement="explicit_destructive_request",
        schema=EVENT_SCHEMA,
        authorization_context_resolver=resolve_target,
    )

    result = asyncio.run(
        registry.execute(
            context(
                "删掉吧",
                semantic_turns=(
                    AuthorizationSemanticTurn(
                        "user", "明天下午那个项目组会是几点？"
                    ),
                    AuthorizationSemanticTurn(
                        "assistant", "项目组会是15:00到16:00。"
                    ),
                    AuthorizationSemanticTurn("user", "删掉吧"),
                ),
            ),
            "calendar_delete_event",
            {"event_id": "provider-event-secret"},
        )
    )

    assert result.status == "succeeded"
    assert deleted == ["provider-event-secret"]
    proposal = verifier.calls[0]
    serialized = json.dumps(proposal, ensure_ascii=False)
    assert proposal["proposal_summary"]["exact_target_supplied"] is True
    assert proposal["proposal_summary"]["target"] == {
        "summary": "项目组会",
        "start_time": "2026-09-08T15:00:00+08:00",
        "end_time": "2026-09-08T16:00:00+08:00",
        "recurrence": "FREQ=WEEKLY",
    }
    assert proposal["semantic_turn_context"] == (
        {"role": "user", "text": "明天下午那个项目组会是几点？"},
        {"role": "assistant", "text": "项目组会是15:00到16:00。"},
        {"role": "user", "text": "删掉吧"},
    )
    assert "event_id" not in serialized
    assert "calendar_id" not in serialized
    assert "provider_id" not in serialized
    assert "provider-event-secret" not in serialized
    assert "open-sensitive" not in serialized
    assert "chat-sensitive" not in serialized
    assert "P-SENSITIVE" not in serialized


def test_delete_target_mismatch_is_denied_before_handler():
    verifier = StaticVerifier(
        MutationIntentDecision("deny", "ambiguous", "target_mismatch")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    deleted = []

    async def resolve_target(_ctx, _args):
        return {
            "target": {
                "summary": "牙医预约",
                "start_time": "2026-09-08T15:00:00+08:00",
                "end_time": "2026-09-08T16:00:00+08:00",
                "recurrence": "",
            }
        }

    register(
        registry,
        "calendar_delete_event",
        lambda _ctx, args: deleted.append(args) or {"ok": True},
        effect="destructive_external_write",
        authorization_requirement="explicit_destructive_request",
        schema=EVENT_SCHEMA,
        authorization_context_resolver=resolve_target,
    )

    result = asyncio.run(
        registry.execute(
            context(
                "删掉吧",
                semantic_turns=(
                    AuthorizationSemanticTurn(
                        "user", "明天下午那个项目组会是几点？"
                    ),
                    AuthorizationSemanticTurn(
                        "assistant", "项目组会是15:00到16:00。"
                    ),
                    AuthorizationSemanticTurn("user", "删掉吧"),
                ),
            ),
            "calendar_delete_event",
            {"event_id": "wrong-provider-event"},
        )
    )

    assert result.status == "tool_effect_not_authorized"
    assert result.result["reason_code"] == "target_mismatch"
    assert verifier.calls[0]["proposal_summary"]["target"]["summary"] == "牙医预约"
    assert deleted == []


def test_multiturn_delete_with_two_possible_targets_needs_clarification():
    verifier = StaticVerifier(
        MutationIntentDecision(
            "needs_clarification", "ambiguous", "multiple_targets"
        )
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    deleted = []

    async def resolve_target(_ctx, _args):
        return {
            "target": {
                "summary": "项目组会",
                "start_time": "2026-09-08T15:00:00+08:00",
                "end_time": "2026-09-08T16:00:00+08:00",
                "recurrence": "",
            }
        }

    register(
        registry,
        "calendar_delete_event",
        lambda _ctx, args: deleted.append(args) or {"ok": True},
        effect="destructive_external_write",
        authorization_requirement="explicit_destructive_request",
        schema=EVENT_SCHEMA,
        authorization_context_resolver=resolve_target,
    )

    result = asyncio.run(
        registry.execute(
            context(
                "删掉吧",
                semantic_turns=(
                    AuthorizationSemanticTurn(
                        "user", "明天下午两个组会分别几点？"
                    ),
                    AuthorizationSemanticTurn(
                        "assistant", "项目组会15点，导师组会16点。"
                    ),
                    AuthorizationSemanticTurn("user", "删掉吧"),
                ),
            ),
            "calendar_delete_event",
            {"event_id": "one-of-two-events"},
        )
    )

    assert result.status == "mutation_needs_clarification"
    assert result.result["reason_code"] == "multiple_targets"
    assert len(verifier.calls[0]["semantic_turn_context"]) == 3
    assert deleted == []


def test_update_verifier_receives_backend_bound_target_and_requested_change():
    verifier = StaticVerifier(
        MutationIntentDecision("allow", "direct_action", "direct_request")
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    updated = []

    async def resolve_target(_ctx, _args):
        return {
            "target": {
                "summary": "项目组会",
                "start_time": "2026-09-08T14:00:00+08:00",
                "end_time": "2026-09-08T15:00:00+08:00",
                "recurrence": "FREQ=WEEKLY",
            }
        }

    update_schema = {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "minLength": 1},
            "start_time": {"type": "string"},
        },
        "required": ["event_id", "start_time"],
        "additionalProperties": False,
    }
    register(
        registry,
        "calendar_update_event",
        lambda _ctx, args: updated.append(args) or {"ok": True},
        effect="external_write",
        authorization_requirement="direct_request",
        schema=update_schema,
        authorization_context_resolver=resolve_target,
    )

    result = asyncio.run(
        registry.execute(
            context(
                "改到三点",
                semantic_turns=(
                    AuthorizationSemanticTurn(
                        "user", "明天下午的项目组会是几点？"
                    ),
                    AuthorizationSemanticTurn("assistant", "项目组会是14:00。"),
                    AuthorizationSemanticTurn("user", "改到三点"),
                ),
            ),
            "calendar_update_event",
            {
                "event_id": "provider-event-secret",
                "start_time": "2026-09-08T15:00:00+08:00",
            },
        )
    )

    proposal = verifier.calls[0]["proposal_summary"]
    assert result.status == "succeeded"
    assert proposal["target"]["summary"] == "项目组会"
    assert proposal["requested_values"] == {
        "start_time": "2026-09-08T15:00:00+08:00"
    }
    assert verifier.calls[0]["semantic_turn_context"][-1] == {
        "role": "user",
        "text": "改到三点",
    }
    assert updated == [
        {
            "event_id": "provider-event-secret",
            "start_time": "2026-09-08T15:00:00+08:00",
        }
    ]


def test_calendar_authorization_resolver_reads_participant_bound_target():
    class Calendar:
        def __init__(self):
            self.calls = []

        async def get_event(self, participant_id, event_id):
            self.calls.append((participant_id, event_id))
            return {
                "id": event_id,
                "summary": "项目组会",
                "start_time": "2026-09-08T07:00:00+00:00",
                "end_time": "2026-09-08T08:00:00+00:00",
                "recurrence": "FREQ=WEEKLY",
                "calendar_id": "private-calendar",
                "recurring_event_id": "private-series",
            }

    calendar = Calendar()
    tools = CareTools(None, None, calendar, None, "Asia/Shanghai", None)
    ctx = context("把项目组会改到三点")

    resolved = asyncio.run(
        tools.resolve_calendar_event_authorization_context(
            ctx, {"event_id": "provider-event-secret"}
        )
    )

    assert calendar.calls == [(ctx.participant_id, "provider-event-secret")]
    assert resolved == {
        "target": {
            "summary": "项目组会",
            "start_time": "2026-09-08T15:00:00+08:00",
            "end_time": "2026-09-08T16:00:00+08:00",
            "recurrence": "FREQ=WEEKLY",
            "timezone": "Asia/Shanghai",
            "scope_kind": "recurring_occurrence",
        }
    }
    serialized = json.dumps(resolved, ensure_ascii=False)
    assert "provider-event-secret" not in serialized
    assert "private-calendar" not in serialized
    assert "private-series" not in serialized


@pytest.mark.parametrize(
    ("event_scope_fields", "expected_scope"),
    (
        (
            {"recurrence": "", "recurring_event_id": "", "is_exception": False},
            "single_event",
        ),
        (
            {
                "recurrence": "FREQ=WEEKLY;INTERVAL=1",
                "recurring_event_id": "",
                "is_exception": False,
            },
            "recurring_series",
        ),
        (
            {
                "recurrence": "",
                "recurring_event_id": "private-series-id",
                "is_exception": False,
            },
            "recurring_occurrence",
        ),
        (
            {
                "recurrence": "",
                "recurring_event_id": "private-series-id",
                "is_exception": True,
            },
            "recurring_exception_occurrence",
        ),
    ),
)
def test_calendar_authorization_scope_is_backend_derived(
    event_scope_fields, expected_scope
):
    class Calendar:
        async def get_event(self, _participant_id, event_id):
            return {
                "id": event_id,
                "summary": "项目组会",
                "start_time": "2026-09-09T07:00:00+00:00",
                "end_time": "2026-09-09T08:00:00+00:00",
                **event_scope_fields,
            }

    tools = CareTools(None, None, Calendar(), None, "Asia/Shanghai", None)
    verifier = StaticVerifier(
        MutationIntentDecision(
            "allow", "destructive_action", "explicit_destructive_request"
        )
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    register(
        registry,
        "calendar_delete_event",
        lambda _ctx, _args: {"ok": True},
        effect="destructive_external_write",
        authorization_requirement="explicit_destructive_request",
        schema=EVENT_SCHEMA,
        authorization_context_resolver=(
            tools.resolve_calendar_event_authorization_context
        ),
    )

    result = asyncio.run(
        registry.execute(
            context("删除这个日程"),
            "calendar_delete_event",
            {"event_id": "private-event-id"},
        )
    )

    assert result.status == "succeeded"
    proposal = verifier.calls[0]["proposal_summary"]
    assert proposal["target"]["scope_kind"] == expected_scope
    serialized = json.dumps(verifier.calls[0], ensure_ascii=False)
    assert "private-event-id" not in serialized
    assert "private-series-id" not in serialized
    assert "recurring_event_id" not in serialized


@pytest.mark.parametrize(
    ("user_text", "scope_kind"),
    (
        ("只删掉这周三这一场", "recurring_series"),
        ("以后每周这个组会都删掉", "recurring_occurrence"),
    ),
)
def test_delete_recurrence_scope_mismatch_never_executes(
    user_text, scope_kind
):
    verifier = StaticVerifier(
        MutationIntentDecision(
            "needs_clarification", "ambiguous", "recurrence_scope_mismatch"
        )
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    deleted = []

    async def resolve_target(_ctx, _args):
        return {
            "target": {
                "summary": "项目组会",
                "start_time": "2026-09-09T15:00:00+08:00",
                "end_time": "2026-09-09T16:00:00+08:00",
                "recurrence": "FREQ=WEEKLY;INTERVAL=1",
                "timezone": "Asia/Shanghai",
                "scope_kind": scope_kind,
            }
        }

    register(
        registry,
        "calendar_delete_event",
        lambda _ctx, args: deleted.append(args) or {"ok": True},
        effect="destructive_external_write",
        authorization_requirement="explicit_destructive_request",
        schema=EVENT_SCHEMA,
        authorization_context_resolver=resolve_target,
    )

    result = asyncio.run(
        registry.execute(
            context(user_text),
            "calendar_delete_event",
            {"event_id": "private-event-id"},
        )
    )

    assert result.status == "mutation_needs_clarification"
    assert result.result["reason_code"] == "recurrence_scope_mismatch"
    proposal = verifier.calls[0]["proposal_summary"]
    assert proposal["target"]["scope_kind"] == scope_kind
    assert "private-event-id" not in json.dumps(proposal, ensure_ascii=False)
    assert deleted == []


def test_update_recurrence_scope_mismatch_never_executes():
    verifier = StaticVerifier(
        MutationIntentDecision(
            "needs_clarification", "ambiguous", "recurrence_scope_mismatch"
        )
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    updated = []

    async def resolve_target(_ctx, _args):
        return {
            "target": {
                "summary": "项目组会",
                "start_time": "2026-09-09T15:00:00+08:00",
                "end_time": "2026-09-09T16:00:00+08:00",
                "recurrence": "FREQ=WEEKLY;INTERVAL=1",
                "timezone": "Asia/Shanghai",
                "scope_kind": "recurring_series",
            }
        }

    update_schema = {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "minLength": 1},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
        },
        "required": ["event_id", "start_time", "end_time"],
        "additionalProperties": False,
    }
    register(
        registry,
        "calendar_update_event",
        lambda _ctx, args: updated.append(args) or {"ok": True},
        effect="external_write",
        authorization_requirement="direct_request",
        schema=update_schema,
        authorization_context_resolver=resolve_target,
    )

    result = asyncio.run(
        registry.execute(
            context("只把本周这一场改到四点"),
            "calendar_update_event",
            {
                "event_id": "private-event-id",
                "start_time": "2026-09-09T16:00:00+08:00",
                "end_time": "2026-09-09T17:00:00+08:00",
            },
        )
    )

    assert result.status == "mutation_needs_clarification"
    assert result.result["reason_code"] == "recurrence_scope_mismatch"
    proposal = verifier.calls[0]["proposal_summary"]
    assert proposal["target"]["scope_kind"] == "recurring_series"
    assert proposal["requested_values"] == {
        "start_time": "2026-09-09T16:00:00+08:00",
        "end_time": "2026-09-09T17:00:00+08:00",
    }
    assert updated == []


@pytest.mark.parametrize(
    ("resolver_error", "reason_code"),
    (
        (
            AuthorizationContextResolutionError("authorization_target_not_found"),
            "authorization_target_not_found",
        ),
        (
            AuthorizationContextResolutionError("calendar_not_connected"),
            "calendar_not_connected",
        ),
        (RuntimeError("backend read failed"), "authorization_context_failure"),
    ),
)
def test_authorization_context_resolution_failure_fails_closed(
    resolver_error, reason_code
):
    verifier = StaticVerifier(error=AssertionError("verifier must not run"))
    registry = ToolRegistry(mutation_verifier=verifier)
    handled = []

    async def failed_resolver(_ctx, _args):
        raise resolver_error

    register(
        registry,
        "calendar_delete_event",
        lambda _ctx, args: handled.append(args) or {"ok": True},
        effect="destructive_external_write",
        authorization_requirement="explicit_destructive_request",
        schema=EVENT_SCHEMA,
        authorization_context_resolver=failed_resolver,
    )

    result = asyncio.run(
        registry.execute(
            context("删除项目组会"),
            "calendar_delete_event",
            {"event_id": "provider-event"},
        )
    )

    assert result.status == "authorization_unavailable"
    assert result.result == {
        "ok": False,
        "error": "mutation_authorization_unavailable",
        "reason_code": reason_code,
    }
    assert verifier.calls == []
    assert handled == []


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


def test_openai_compatible_verifier_uses_configured_completion_budget(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "decision": "allow",
                "intent": "direct_action",
                "reason_code": "direct_request",
            }

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(mutation_intent_verifier_module.requests, "post", post)
    client = mutation_intent_verifier_module.OpenAICompatibleMutationIntentClient(
        "https://provider.example/v1/chat/completions",
        "test-key",
        "test-model",
        max_tokens=1024,
    )

    result = client.infer({"user_request_text": "请创建日程"})

    assert result["decision"] == "allow"
    assert captured["url"] == "https://provider.example/v1/chat/completions"
    assert captured["json"]["thinking"] == {"type": "disabled"}
    assert captured["json"]["max_tokens"] == 1024


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
            semantic_turn_context=(
                {
                    "role": "user",
                    "text": (
                        "participant_id=participant-secret "
                        "participant_code=code-secret open_id=open-secret "
                        "chat_id=chat-secret event_id=event-secret"
                    ),
                },
                {
                    "role": "assistant",
                    "text": (
                        "calendar_id=calendar-secret token=turn-token "
                        "学号=20261234 provider id=provider-secret"
                    ),
                },
            ),
        )
    )

    serialized = json.dumps(client.payloads[0], ensure_ascii=False)
    assert "top-secret" not in serialized
    assert "20260001" not in serialized
    assert "provider-event-secret" not in serialized
    assert "provider-token" not in serialized
    assert "participant-secret" not in serialized
    assert "code-secret" not in serialized
    assert "open-secret" not in serialized
    assert "chat-secret" not in serialized
    assert "event-secret" not in serialized
    assert "calendar-secret" not in serialized
    assert "turn-token" not in serialized
    assert "20261234" not in serialized
    assert "provider-secret" not in serialized


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


@pytest.mark.parametrize(
    ("decision", "intent", "authorization_requirement"),
    itertools.product(
        ("allow", "deny", "needs_clarification"),
        (
            "direct_action",
            "destructive_action",
            "status_query",
            "capability_question",
            "hypothetical",
            "ambiguous",
            "cancel_or_revert",
        ),
        ("direct_request", "explicit_destructive_request"),
    ),
)
def test_verifier_decision_intent_authorization_matrix(
    decision, intent, authorization_requirement
):
    tool_name = (
        "course_schedule_cancel_or_revert_import"
        if authorization_requirement == "direct_request"
        else "calendar_delete_event"
    )
    verifier = MutationIntentVerifier(
        RawClient(
            {
                "decision": decision,
                "intent": intent,
                "reason_code": "matrix_case",
            }
        )
    )
    expected_valid = (
        decision == "deny"
        or (decision == "needs_clarification" and intent == "ambiguous")
        or (
            decision == "allow"
            and (
                (
                    authorization_requirement == "direct_request"
                    and intent in {"direct_action", "cancel_or_revert"}
                )
                or (
                    authorization_requirement == "explicit_destructive_request"
                    and intent == "destructive_action"
                )
            )
        )
    )
    kwargs = {
        "user_request_text": "清理这次导入",
        "tool_name": tool_name,
        "tool_effect": "internal_write",
        "authorization_requirement": authorization_requirement,
        "proposal_summary": {"proposed_operation": tool_name},
        "source_kind": "text",
    }

    if expected_valid:
        parsed = asyncio.run(verifier.verify(**kwargs))
        assert (parsed.decision, parsed.intent) == (decision, intent)
    else:
        with pytest.raises(MutationIntentVerificationError):
            asyncio.run(verifier.verify(**kwargs))


def test_verifier_normalizes_observed_calendar_create_intent_alias():
    verifier = MutationIntentVerifier(
        RawClient(
            {
                "decision": "allow",
                "intent": "create_calendar_event",
                "reason_code": "direct_request_exact_target",
            }
        )
    )

    decision = asyncio.run(
        verifier.verify(
            user_request_text="请在 9 月 11 日创建日程",
            tool_name="calendar_create_event",
            tool_effect="external_write",
            authorization_requirement="direct_request",
            proposal_summary={"operation_type": "create"},
            source_kind="text",
        )
    )

    assert decision.decision == "allow"
    assert decision.intent == "direct_action"
    assert decision.reason_code == "direct_request_exact_target"


def test_verifier_prompt_requires_canonical_intent_labels():
    prompt = mutation_intent_verifier_module.OpenAICompatibleMutationIntentClient.SYSTEM_PROMPT

    assert "The intent value must be exactly one of" in prompt
    assert "Never use a tool name or operation-specific value" in prompt
