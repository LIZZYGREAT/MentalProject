import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.repositories import ObservationRepository
from app.repositories_calendar_plan import CalendarMutationPlanRepository
from app.services.card_action_service import CardActionService
from app.services.mutation_intent_verifier import MutationIntentDecision
from app.services.presentation_service import PresentationOutbox
from app.tools.care import CareTools
from helpers import memory_database, participant


class _Verifier:
    def __init__(self):
        self.calls = []

    async def verify(self, **kwargs):
        self.calls.append(kwargs)
        return MutationIntentDecision("allow", "batch_scope_matches", "direct_request")


class _Calendar:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.events = {
            "weekend-1": {
                "id": "weekend-1",
                "summary": "军训服回收志愿服务",
                "start_time": "2030-01-12T17:30:00+08:00",
                "end_time": "2030-01-12T19:00:00+08:00",
            },
            "weekend-2": {
                "id": "weekend-2",
                "summary": "军训服回收志愿服务",
                "start_time": "2030-01-13T17:30:00+08:00",
                "end_time": "2030-01-13T19:00:00+08:00",
            },
        }

    async def create_single_event(self, participant_id, **kwargs):
        self.created.append((participant_id, kwargs))
        return {"id": f"created-{len(self.created)}", **kwargs}

    async def get_event(self, _participant_id, event_id):
        return dict(self.events[event_id])

    async def delete_event(self, participant_id, event_id):
        self.deleted.append((participant_id, event_id))
        return True


def _context(participant_id, text):
    return AgentContext(
        participant_id=participant_id,
        participant_code="P-PLAN",
        open_id="open",
        chat_id="chat",
        message_id="source-message",
        agent_run_id=uuid.uuid4(),
        user_request_text=text,
    )


def _stack(database, calendar, verifier):
    plans = CalendarMutationPlanRepository(database)
    outbox = PresentationOutbox()
    tools = CareTools(
        None,
        None,
        calendar,
        None,
        "Asia/Shanghai",
        None,
        presentations=outbox,
        calendar_mutation_plans=plans,
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    tools.register(registry)
    return plans, outbox, tools, registry


def _card_service(database, tools):
    return CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(
            on_observation_committed=lambda **_values: None
        ),
        calendar_mutation_plan_executor=tools.execute_calendar_mutation_plan,
    )


def _plan_action(card, suffix):
    for element in card["body"]["elements"]:
        if element.get("tag") != "button":
            continue
        action = element["behaviors"][0]["value"]
        if action["mindflow_action"].endswith(suffix):
            return action
    raise AssertionError(f"missing plan action: {suffix}")


def test_plan_repository_is_participant_bound_single_claim_and_expires():
    database = memory_database()
    owner = participant(database, "PLAN-OWNER")
    other = participant(database, "PLAN-OTHER")
    repository = CalendarMutationPlanRepository(database)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    plan = repository.create(
        owner.id,
        operation="delete",
        items=[{"event_id": "a"}, {"event_id": "b"}],
        now=now,
    )

    assert repository.claim(other.id, plan["id"], now=now) is None
    assert repository.claim(owner.id, plan["id"], now=now)["claim_status"] == "claimed"
    assert repository.claim(owner.id, plan["id"], now=now)["claim_status"] == "processing"

    expired = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": "a"}, {"summary": "b"}],
        ttl_minutes=1,
        now=now,
    )
    assert repository.claim(
        owner.id, expired["id"], now=now + timedelta(minutes=2)
    )["claim_status"] == "expired"


def test_multi_date_create_uses_one_plan_card_and_executes_once():
    database = memory_database()
    owner = participant(database, "PLAN-CREATE")
    calendar = _Calendar()
    verifier = _Verifier()
    _plans, outbox, tools, registry = _stack(database, calendar, verifier)
    ctx = _context(owner.id, "这周六周天晚上都加一个志愿活动")
    events = [
        {
            "summary": "志愿活动",
            "start_time": "2030-01-12T17:30:00+08:00",
            "end_time": "2030-01-12T19:00:00+08:00",
        },
        {
            "summary": "志愿活动",
            "start_time": "2030-01-13T17:30:00+08:00",
            "end_time": "2030-01-13T19:00:00+08:00",
        },
    ]

    planned = asyncio.run(
        registry.execute(ctx, "calendar_create_events_plan", {"events": events})
    )

    assert planned.result["calendar_mutation"] == "pending_confirmation"
    assert calendar.created == []
    assert len(verifier.calls) == 1
    assert len(
        verifier.calls[0]["proposal_summary"]["requested_values"]["events"]
    ) == 2
    cards = outbox.take_cards(ctx.agent_run_id)
    assert len(cards) == 1
    confirm = _plan_action(cards[0], "_confirm")
    assert "events" not in confirm and "event_ids" not in confirm

    service = _card_service(database, tools)
    first = service.handle(
        owner.id,
        message_id="plan-card",
        callback_event_id="confirm-create",
        action_value=confirm,
        form_value={},
    )
    second = service.handle(
        owner.id,
        message_id="plan-card",
        callback_event_id="confirm-create-retry",
        action_value=confirm,
        form_value={},
    )

    assert first["succeeded_count"] == 2
    assert len(calendar.created) == 2
    assert second["already_handled"] is True
    assert len(calendar.created) == 2


def test_multi_event_delete_binds_all_targets_to_one_card_before_deleting():
    database = memory_database()
    owner = participant(database, "PLAN-DELETE")
    calendar = _Calendar()
    verifier = _Verifier()
    _plans, outbox, tools, registry = _stack(database, calendar, verifier)
    ctx = _context(owner.id, "删除刚加入的两个周末事件")

    planned = asyncio.run(
        registry.execute(
            ctx,
            "calendar_delete_events_plan",
            {"event_ids": ["weekend-1", "weekend-2"]},
        )
    )

    assert planned.result["item_count"] == 2
    assert calendar.deleted == []
    assert len(verifier.calls) == 1
    proposal = verifier.calls[0]["proposal_summary"]
    assert proposal["exact_target_count"] == 2
    assert "weekend-1" not in json.dumps(proposal)
    card = outbox.take_cards(ctx.agent_run_id)[0]
    serialized = json.dumps(card, ensure_ascii=False)
    assert "weekend-1" not in serialized and "weekend-2" not in serialized

    result = _card_service(database, tools).handle(
        owner.id,
        message_id="delete-plan-card",
        callback_event_id="confirm-delete",
        action_value=_plan_action(card, "_confirm"),
        form_value={},
    )

    assert result["succeeded_count"] == 2
    assert calendar.deleted == [
        (owner.id, "weekend-1"),
        (owner.id, "weekend-2"),
    ]
