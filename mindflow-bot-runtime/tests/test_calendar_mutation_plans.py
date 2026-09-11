import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.calendar import CalendarMutationOutcomeUnknown
from app.repositories import ObservationRepository
from app.repositories_calendar_plan import CalendarMutationPlanRepository
from app.services.calendar_mutation_plan_runner import CalendarMutationPlanRunner
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
    runner = CalendarMutationPlanRunner(plans, tools.execute_calendar_mutation_plan_item)
    tools.calendar_mutation_plan_notifier = runner.wake
    return plans, outbox, tools, registry, runner


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
    assert repository.claim(owner.id, plan["id"], now=now)["claim_status"] == "queued"

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
    _plans, outbox, tools, registry, runner = _stack(database, calendar, verifier)
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

    assert first["status"] == "queued"
    asyncio.run(runner.run_once())
    assert len(calendar.created) == 2
    assert second["already_handled"] is True
    assert len(calendar.created) == 2


def test_multi_event_delete_binds_all_targets_to_one_card_before_deleting():
    database = memory_database()
    owner = participant(database, "PLAN-DELETE")
    calendar = _Calendar()
    verifier = _Verifier()
    _plans, outbox, tools, registry, runner = _stack(database, calendar, verifier)
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

    assert result["status"] == "queued"
    asyncio.run(runner.run_once())
    assert calendar.deleted == [
        (owner.id, "weekend-1"),
        (owner.id, "weekend-2"),
    ]


def _durable_plan(repository, participant_id, operation, items, now):
    plan = repository.create(
        participant_id, operation=operation, items=items, now=now
    )
    repository.request_execution(participant_id, plan["id"], now=now)
    return plan


def test_create_crash_recovery_skips_succeeded_item_and_resumes_next():
    database = memory_database()
    owner = participant(database, "PLAN-CREATE-RECOVERY")
    repository = CalendarMutationPlanRepository(database)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    plan = _durable_plan(
        repository,
        owner.id,
        "create",
        [
            {"summary": "first", "start_time": "2030-01-02T08:00:00+08:00", "end_time": "2030-01-02T09:00:00+08:00"},
            {"summary": "second", "start_time": "2030-01-03T08:00:00+08:00", "end_time": "2030-01-03T09:00:00+08:00"},
        ],
        now,
    )
    provider_events = {}
    provider_calls = []

    async def execute(_plan, item):
        source = item["source_identity"]
        provider_calls.append(source)
        provider_events.setdefault(source, f"event-{len(provider_events) + 1}")
        return {"ok": True, "created": {"id": provider_events[source]}}

    crashed_owner = "crashed-runner"
    claimed_plan = repository.claim_next_plan(
        lease_owner=crashed_owner, lease_seconds=5, now=now
    )
    first = repository.claim_next_item(
        plan["id"], lease_owner=crashed_owner, lease_seconds=5, now=now
    )
    first_result = asyncio.run(execute(claimed_plan, first))
    repository.record_item_success(
        plan["id"],
        first["id"],
        lease_owner=crashed_owner,
        provider_event_id=first_result["created"]["id"],
        now=now,
    )
    repository.claim_next_item(
        plan["id"], lease_owner=crashed_owner, lease_seconds=5, now=now
    )
    repository.recover_stale(now=now + timedelta(seconds=6))

    runner = CalendarMutationPlanRunner(repository, execute)
    asyncio.run(runner.run_once())

    final = repository.get(plan["id"])
    assert final["status"] == "succeeded"
    assert [item["status"] for item in final["ledger_items"]] == [
        "succeeded",
        "succeeded",
    ]
    assert len(provider_events) == 2
    assert provider_calls.count(first["source_identity"]) == 1


def test_delete_crash_recovery_does_not_repeat_completed_delete():
    database = memory_database()
    owner = participant(database, "PLAN-DELETE-RECOVERY")
    repository = CalendarMutationPlanRepository(database)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    plan = _durable_plan(
        repository,
        owner.id,
        "delete",
        [{"event_id": "first"}, {"event_id": "second"}],
        now,
    )
    existing = {"first", "second"}
    provider_calls = []

    async def execute(_plan, item):
        event_id = item["payload"]["event_id"]
        provider_calls.append(event_id)
        existing.discard(event_id)
        return {"ok": True, "deleted": True}

    crashed_owner = "crashed-delete-runner"
    claimed_plan = repository.claim_next_plan(
        lease_owner=crashed_owner, lease_seconds=5, now=now
    )
    first = repository.claim_next_item(
        plan["id"], lease_owner=crashed_owner, lease_seconds=5, now=now
    )
    asyncio.run(execute(claimed_plan, first))
    repository.record_item_success(
        plan["id"], first["id"], lease_owner=crashed_owner, now=now
    )
    repository.claim_next_item(
        plan["id"], lease_owner=crashed_owner, lease_seconds=5, now=now
    )
    repository.recover_stale(now=now + timedelta(seconds=6))

    asyncio.run(CalendarMutationPlanRunner(repository, execute).run_once())

    assert repository.get(plan["id"])["status"] == "succeeded"
    assert existing == set()
    assert provider_calls == ["first", "second"]


def test_outcome_unknown_is_reconciled_with_stable_source_identity():
    database = memory_database()
    owner = participant(database, "PLAN-UNKNOWN")
    repository = CalendarMutationPlanRepository(database)
    plan = _durable_plan(
        repository,
        owner.id,
        "create",
        [{"summary": "first"}, {"summary": "second"}],
        datetime.now(timezone.utc),
    )
    events = {}
    attempts = {}

    async def execute(_plan, item):
        source = item["source_identity"]
        attempts[source] = attempts.get(source, 0) + 1
        events.setdefault(source, f"event-{len(events) + 1}")
        if item["item_index"] == 0 and attempts[source] == 1:
            raise CalendarMutationOutcomeUnknown(
                "response lost", request_kind="create_event"
            )
        return {"ok": True, "created": {"id": events[source]}}

    runner = CalendarMutationPlanRunner(repository, execute)
    asyncio.run(runner.run_once())
    intermediate = repository.get(plan["id"])
    assert intermediate["status"] == "recovery_required"
    assert intermediate["ledger_items"][0]["status"] == "outcome_unknown"

    asyncio.run(runner.run_once())
    final = repository.get(plan["id"])
    assert final["status"] == "succeeded"
    assert len(events) == 2
    assert final["ledger_items"][0]["attempt_count"] == 2


def test_deterministic_item_failure_yields_partial_failed_without_rollback():
    database = memory_database()
    owner = participant(database, "PLAN-PARTIAL")
    repository = CalendarMutationPlanRepository(database)
    plan = _durable_plan(
        repository,
        owner.id,
        "create",
        [{"summary": "first"}, {"summary": "second"}],
        datetime.now(timezone.utc),
    )
    calls = []

    async def execute(_plan, item):
        calls.append(item["item_index"])
        if item["item_index"] == 1:
            return {"ok": False, "error": "provider_rejected"}
        return {"ok": True, "created": {"id": "created-first"}}

    asyncio.run(CalendarMutationPlanRunner(repository, execute).run_once())

    final = repository.get(plan["id"])
    assert final["status"] == "partial_failed"
    assert final["result"] == {
        "succeeded_count": 1,
        "failed_count": 1,
        "errors": ["provider_rejected"],
    }
    assert calls == [0, 1]
