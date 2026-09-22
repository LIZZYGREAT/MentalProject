import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, select

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.calendar import (
    CalendarMutationOutcomeUnknown,
    CalendarProviderUnavailable,
)
from app.integrations.feishu.client import FeishuSendError
from app.models import CalendarMutationPlan, CalendarMutationPlanItem, RuntimeIncident
from app.repositories import ObservationRepository
from app.repositories_calendar_plan import CalendarMutationPlanRepository
from app.services.calendar_mutation_plan_runner import CalendarMutationPlanRunner
from app.services.card_action_service import CardActionService, _aware_calendar_datetime
from app.services.course_series_resolver import ResolvedCourseSeries
from app.services.mutation_intent_verifier import MutationIntentDecision
from app.services.presentation_service import PresentationOutbox
from app.services.runtime_clock import RuntimeClock
from app.tools.care import CareTools
from helpers import memory_database, participant


class _Verifier:
    def __init__(self, decision=None):
        self.calls = []
        self.decision = decision or MutationIntentDecision(
            "allow", "direct_action", "batch_scope_matches"
        )

    async def verify(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


class _Calendar:
    def __init__(self):
        self.created = []
        self.updated = []
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

    async def create_recurring_event(self, participant_id, **kwargs):
        self.created.append((participant_id, kwargs))
        return {"id": f"created-{len(self.created)}", **kwargs}

    async def get_event(self, _participant_id, event_id):
        return dict(self.events[event_id])

    async def delete_event(self, participant_id, event_id):
        self.deleted.append((participant_id, event_id))
        return True

    async def update_event(self, participant_id, event_id, **kwargs):
        self.updated.append((participant_id, event_id, kwargs))
        updated = {**self.events[event_id]}
        updated.update({key: value for key, value in kwargs.items() if value is not None})
        self.events[event_id] = updated
        return updated


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


def _stack(database, calendar, verifier, *, clock=None):
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
        clock=clock,
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    tools.register(registry)
    runner = CalendarMutationPlanRunner(plans, tools.execute_calendar_mutation_plan_item)
    tools.calendar_mutation_plan_notifier = runner.wake
    return plans, outbox, tools, registry, runner


class _ReferenceDateResolver:
    def __init__(self, calendar):
        self.calendar = calendar
        self.reference_local_dates = []

    async def resolve(
        self,
        _participant_id,
        *,
        anchor_event,
        reference_local_date,
        **_kwargs,
    ):
        self.reference_local_dates.append(reference_local_date)
        return ResolvedCourseSeries(
            anchor_event_id=str(anchor_event["id"]),
            course_identity="course-import:import-1:item-1",
            display_name=str(anchor_event["summary"]),
            scope_start=datetime.fromisoformat(anchor_event["start_time"]),
            scope_end=datetime.fromisoformat(anchor_event["end_time"]),
            occurrence_events=(dict(anchor_event),),
            resolution_source="course_import",
        )


def _stage_course_series_with_reference_times(*, received_at_utc, worker_now):
    database = memory_database()
    owner = participant(database, f"PLAN-REFERENCE-{uuid.uuid4().hex[:8]}")
    calendar = _Calendar()
    calendar.events["course-reference"] = {
        "id": "course-reference",
        "summary": "操作系统(0955)",
        "start_time": "2026-09-22T12:55:00+08:00",
        "end_time": "2026-09-22T14:30:00+08:00",
    }
    clock = RuntimeClock("Asia/Shanghai", now_fn=lambda: worker_now)
    _plans, _outbox, tools, _registry, _runner = _stack(
        database,
        calendar,
        _Verifier(),
        clock=clock,
    )
    resolver = _ReferenceDateResolver(calendar)
    tools.course_series_resolver = resolver
    ctx = _context(owner.id, "以后这个课下课时间改为15:40")
    ctx = AgentContext(
        **{
            **ctx.__dict__,
            "received_at_utc": received_at_utc,
        }
    )

    result = asyncio.run(
        tools.update_calendar_event(
            ctx,
            {
                "event_id": "course-reference",
                "scope": "current_semester_remainder",
                "end_clock": "15:40",
            },
        )
    )
    assert result["ok"] is True
    return resolver.reference_local_dates


def test_course_series_scope_uses_message_received_date():
    reference_dates = _stage_course_series_with_reference_times(
        received_at_utc=datetime(2026, 9, 15, 7, 30, tzinfo=timezone.utc),
        worker_now=datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
    )

    assert reference_dates == [datetime(2026, 9, 15).date()]


def test_course_series_scope_does_not_cross_midnight_with_worker_delay():
    reference_dates = _stage_course_series_with_reference_times(
        received_at_utc=datetime(2026, 9, 15, 15, 59, tzinfo=timezone.utc),
        worker_now=datetime(2026, 9, 15, 16, 1, tzinfo=timezone.utc),
    )

    assert reference_dates == [datetime(2026, 9, 15, 23, 59).date()]


def test_course_series_scope_falls_back_to_injected_clock():
    reference_dates = _stage_course_series_with_reference_times(
        received_at_utc=None,
        worker_now=datetime(2026, 9, 15, 16, 1, tzinfo=timezone.utc),
    )

    assert reference_dates == [datetime(2026, 9, 16).date()]


def _card_service(database, tools):
    return CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(
            on_observation_committed=lambda **_values: None
        ),
        calendar_mutation_plan_executor=tools.execute_calendar_mutation_plan,
        calendar_mutation_plans=tools.calendar_mutation_plans,
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


def test_plan_accepts_one_to_twenty_items_and_update_operation():
    database = memory_database()
    owner = participant(database, "PLAN-CARDINALITY")
    repository = CalendarMutationPlanRepository(database)

    single = repository.create(
        owner.id,
        operation="update",
        items=[{"event_id": "one", "summary": "updated"}],
    )
    twenty = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": str(index)} for index in range(20)],
    )

    assert single["operation"] == "update"
    assert len(single["ledger_items"]) == 1
    assert len(twenty["ledger_items"]) == 20
    with pytest.raises(ValueError, match="1 to 20"):
        repository.create(owner.id, operation="delete", items=[])
    with pytest.raises(ValueError, match="1 to 20"):
        repository.create(
            owner.id,
            operation="delete",
            items=[{"event_id": str(index)} for index in range(21)],
        )


def test_course_series_internal_cap_is_not_generic_agent_batch_cap():
    database = memory_database()
    owner = participant(database, "PLAN-COURSE-INTERNAL-CAP")
    repository = CalendarMutationPlanRepository(database)
    items = [{"event_id": str(index)} for index in range(21)]

    with pytest.raises(ValueError, match="1 to 20"):
        repository.create(owner.id, operation="update", items=items)

    plan = repository.create_course_series(
        owner.id,
        operation="update",
        items=items,
        presentation_context={"kind": "course_series_update"},
    )

    assert len(plan["ledger_items"]) == 21


def _stage_long_course_series(count):
    database = memory_database()
    owner = participant(database, f"PLAN-LONG-COURSE-{uuid.uuid4().hex[:8]}")
    calendar = _Calendar()
    events = []
    first = datetime(2030, 1, 1, 12, 55, tzinfo=timezone(timedelta(hours=8)))
    for index in range(count):
        start = first + timedelta(weeks=index)
        event = {
            "id": f"long-course-{index + 1}",
            "summary": "长期课程",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=95)).isoformat(),
        }
        events.append(event)
        calendar.events[event["id"]] = event
    plans, outbox, tools, _registry, _runner = _stack(
        database, calendar, _Verifier()
    )

    class _Resolver:
        async def resolve(self, *_args, **_kwargs):
            return ResolvedCourseSeries(
                anchor_event_id=events[0]["id"],
                course_identity="course-import:long:item-1",
                display_name="长期课程",
                scope_start=datetime.fromisoformat(events[0]["start_time"]),
                scope_end=datetime.fromisoformat(events[-1]["end_time"]),
                occurrence_events=tuple(dict(event) for event in events),
                resolution_source="course_import",
            )

    tools.course_series_resolver = _Resolver()
    ctx = _context(owner.id, "以后这个课下课时间改为15:40")
    result = asyncio.run(
        tools.update_calendar_event(
            ctx,
            {
                "event_id": events[0]["id"],
                "scope": "current_semester_remainder",
                "end_clock": "15:40",
            },
        )
    )
    return database, plans, outbox, ctx, result


def test_course_series_more_than_twenty_occurrences_stages_one_review():
    database, plans, outbox, ctx, result = _stage_long_course_series(24)

    cards = outbox.take_cards(ctx.agent_run_id)
    with database.session() as session:
        plan_id = str(session.scalar(select(CalendarMutationPlan.id)))
    plan = plans.get_for_participant(ctx.participant_id, plan_id)

    assert result["ok"] is True
    assert result["item_count"] == 24
    assert len(plan["ledger_items"]) == 24
    assert len(cards) == 1


def test_course_series_compact_card_shows_full_occurrence_count():
    _database, _plans, outbox, ctx, result = _stage_long_course_series(24)

    cards = outbox.take_cards(ctx.agent_run_id)
    serialized = json.dumps(cards[0], ensure_ascii=False)

    assert result["course_series"]["occurrence_count"] == 24
    assert "**场次：** 24 节" in serialized
    assert serialized.count("calendar_mutation_plan_confirm") == 1


def test_course_series_end_clock_stages_all_occurrences_without_moving_starts():
    database = memory_database()
    owner = participant(database, "PLAN-COURSE-SERIES")
    calendar = _Calendar()
    calendar.events.update(
        {
            "course-1": {
                "id": "course-1",
                "summary": "操作系统(0955)",
                "start_time": "2030-01-15T12:55:00+08:00",
                "end_time": "2030-01-15T14:30:00+08:00",
            },
            "course-2": {
                "id": "course-2",
                "summary": "操作系统(0955)",
                "start_time": "2030-01-22T12:55:00+08:00",
                "end_time": "2030-01-22T14:30:00+08:00",
            },
        }
    )
    plans, outbox, tools, _registry, runner = _stack(
        database, calendar, _Verifier()
    )

    class _Resolver:
        async def resolve(self, *_args, **_kwargs):
            return ResolvedCourseSeries(
                anchor_event_id="course-1",
                course_identity="course-import:import-1:item-1",
                display_name="操作系统(0955)",
                scope_start=datetime.fromisoformat("2030-01-15T12:55:00+08:00"),
                scope_end=datetime.fromisoformat("2030-01-22T14:30:00+08:00"),
                occurrence_events=(
                    dict(calendar.events["course-1"]),
                    dict(calendar.events["course-2"]),
                ),
                resolution_source="course_import",
            )

    tools.course_series_resolver = _Resolver()
    ctx = _context(owner.id, "以后这个课下课时间改为15:40")
    result = asyncio.run(
        tools.update_calendar_event(
            ctx,
            {
                "event_id": "course-1",
                "scope": "current_semester_remainder",
                "end_clock": "15:40",
            },
        )
    )

    assert result["ok"] is True
    assert result["item_count"] == 2
    assert result["operation"] == "update"
    assert result["scope"] == "current_semester_remainder"
    assert result["course_series"]["occurrence_count"] == 2
    assert result["card_queued"] is True
    assert calendar.updated == []
    confirmation = outbox.take_cards(ctx.agent_run_id)[0]
    serialized = json.dumps(confirmation, ensure_ascii=False)
    assert serialized.count("修改信息") == 1
    assert "查看场次" in serialized
    assert "结束时间" in serialized and "14:30" in serialized and "15:40" in serialized
    with database.session() as session:
        plan_id = str(session.scalar(select(CalendarMutationPlan.id)))
    plan = plans.get_for_participant(owner.id, plan_id)
    assert [item["start_time"] for item in plan["items"]] == [
        "2030-01-15T12:55:00+08:00",
        "2030-01-22T12:55:00+08:00",
    ]
    assert [item["end_time"] for item in plan["items"]] == [
        "2030-01-15T15:40:00+08:00",
        "2030-01-22T15:40:00+08:00",
    ]
    assert plan["presentation_context"]["scope"] == "current_semester_remainder"
    assert plan["presentation_context"]["changes"] == {"end_clock": "15:40"}

    service = _card_service(database, tools)
    occurrence_action = _plan_action(confirmation, "occurrences_view")
    occurrence_view = service.handle(
        owner.id,
        message_id="series-card",
        action_value=occurrence_action,
        form_value={},
    )
    assert occurrence_view["navigation_only"] is True
    assert "course-1" not in json.dumps(occurrence_view["card"])
    assert "2030-01-15" in json.dumps(occurrence_view["card"], ensure_ascii=False)

    edit_action = _plan_action(confirmation, "series_edit_open")
    edit_view = service.handle(
        owner.id,
        message_id="series-card",
        action_value=edit_action,
        form_value={},
    )
    form = edit_view["card"]["body"]["elements"][0]
    submit_action = next(
        element["behaviors"][0]["value"]
        for element in form["elements"]
        if element.get("tag") == "button"
    )
    edited = service.handle(
        owner.id,
        message_id="series-card",
        callback_event_id="series-edit-1",
        action_value=submit_action,
        form_value={
            "summary": "操作系统（新）",
            "start_hour": "13",
            "start_minute": "10",
            "end_hour": "15",
            "end_minute": "50",
        },
    )
    assert edited["ok"] is True
    assert calendar.updated == []
    updated = plans.get_for_participant(owner.id, plan_id)
    assert {item["summary"] for item in updated["items"]} == {"操作系统（新）"}
    assert [item["start_time"] for item in updated["items"]] == [
        "2030-01-15T13:10:00+08:00",
        "2030-01-22T13:10:00+08:00",
    ]
    assert [item["end_time"] for item in updated["items"]] == [
        "2030-01-15T15:50:00+08:00",
        "2030-01-22T15:50:00+08:00",
    ]

    confirm = _plan_action(edited["card"], "_confirm")
    queued = service.handle(
        owner.id,
        message_id="series-card",
        callback_event_id="series-confirm-1",
        action_value=confirm,
        form_value={},
    )
    assert queued["status"] == "queued"
    asyncio.run(runner.run_once())
    assert [event_id for _owner, event_id, _kwargs in calendar.updated] == [
        "course-1",
        "course-2",
    ]
    assert all(
        kwargs["start_time"].strftime("%H:%M") == "13:10"
        and kwargs["end_time"].strftime("%H:%M") == "15:50"
        for _owner, _event_id, kwargs in calendar.updated
    )

    duplicate = service.handle(
        owner.id,
        message_id="series-card",
        callback_event_id="series-confirm-2",
        action_value=confirm,
        form_value={},
    )
    assert duplicate["already_handled"] is True
    asyncio.run(runner.run_once())
    assert len(calendar.updated) == 2


def test_backend_uses_structured_calendar_scope_not_raw_user_text():
    database = memory_database()
    owner = participant(database, "PLAN-SCOPE-AUTHORITY")
    calendar = _Calendar()
    calendar.events.update(
        {
            "course-1": {
                "id": "course-1",
                "summary": "操作系统(0955)",
                "start_time": "2030-01-15T12:55:00+08:00",
                "end_time": "2030-01-15T14:30:00+08:00",
            },
            "course-2": {
                "id": "course-2",
                "summary": "操作系统(0955)",
                "start_time": "2030-01-22T12:55:00+08:00",
                "end_time": "2030-01-22T14:30:00+08:00",
            },
        }
    )
    plans, _outbox, tools, registry, _runner = _stack(
        database, calendar, _Verifier()
    )

    class _Resolver:
        async def resolve(self, *_args, **_kwargs):
            return ResolvedCourseSeries(
                anchor_event_id="course-1",
                course_identity="course-import:import-1:item-1",
                display_name="操作系统(0955)",
                scope_start=datetime.fromisoformat("2030-01-15T12:55:00+08:00"),
                scope_end=datetime.fromisoformat("2030-01-22T14:30:00+08:00"),
                occurrence_events=(
                    dict(calendar.events["course-1"]),
                    dict(calendar.events["course-2"]),
                ),
                resolution_source="course_import",
            )

    tools.course_series_resolver = _Resolver()
    result = asyncio.run(
        registry.execute(
            _context(owner.id, "只改这一次，不要改后面的课"),
            "calendar_update_event",
            {
                "event_ref": "course-1",
                "scope": "current_semester_remainder",
                "changes": {"end_clock": "15:40"},
            },
        )
    )

    assert result.result["item_count"] == 2
    with database.session() as session:
        plan_id = str(session.scalar(select(CalendarMutationPlan.id)))
    assert plans.get_for_participant(owner.id, plan_id)["presentation_context"][
        "scope"
    ] == "current_semester_remainder"


def test_update_plan_uses_updating_item_state():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATING-STATE")
    repository = CalendarMutationPlanRepository(database)
    plan = repository.create(
        owner.id,
        operation="update",
        items=[{"event_id": "one", "summary": "updated"}],
    )
    repository.request_execution(owner.id, plan["id"])
    claimed_plan = repository.claim_next_plan(lease_owner="runner")
    item = repository.claim_next_item(
        claimed_plan["id"], lease_owner="runner"
    )

    assert item["operation"] == "update"
    assert item["status"] == "updating"
    completed = repository.record_item_success(
        plan["id"], item["id"], lease_owner="runner"
    )
    assert completed["status"] == "succeeded"


def test_pending_create_plan_info_update_is_atomic_and_participant_bound():
    database = memory_database()
    owner = participant(database, "PLAN-EDIT-OWNER")
    other = participant(database, "PLAN-EDIT-OTHER")
    repository = CalendarMutationPlanRepository(database)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    plan = repository.create(
        owner.id,
        operation="create",
        items=[
            {
                "summary": "first",
                "description": "keep me",
                "start_time": "2030-01-02T08:00:00+08:00",
                "end_time": "2030-01-02T09:00:00+08:00",
            },
            {
                "summary": "second",
                "start_time": "2030-01-03T08:00:00+08:00",
                "end_time": "2030-01-03T09:00:00+08:00",
            },
        ],
        now=now,
    )
    item_id = plan["ledger_items"][0]["id"]
    updated_at = now + timedelta(minutes=1)
    start = datetime.fromisoformat("2030-01-02T10:15:00+08:00")
    end = datetime.fromisoformat("2030-01-02T11:45:00+08:00")

    assert repository.update_pending_item_time(
        other.id,
        plan["id"],
        item_id,
        start_time=start,
        end_time=end,
        now=updated_at,
    ) is None
    result = repository.update_pending_item(
        owner.id,
        plan["id"],
        item_id,
        summary="renamed first",
        start_time=start,
        end_time=end,
        now=updated_at,
    )

    assert result["update_status"] == "updated"
    assert result["items"][0]["summary"] == "renamed first"
    assert result["items"][0]["start_time"] == start.isoformat()
    assert result["items"][0]["end_time"] == end.isoformat()
    assert result["items"][0]["description"] == "keep me"
    assert result["items"][1] == plan["items"][1]
    assert result["ledger_items"][0]["payload"] == result["items"][0]
    assert result["ledger_items"][0]["source_identity"] == (
        plan["ledger_items"][0]["source_identity"]
    )

    # Re-open a session so this assertion covers JSON dirty tracking and commit.
    with database.session() as session:
        stored_plan = session.get(CalendarMutationPlan, uuid.UUID(plan["id"]))
        stored_item = session.get(CalendarMutationPlanItem, uuid.UUID(item_id))
        assert stored_plan.items_json[0]["start_time"] == start.isoformat()
        assert stored_plan.items_json[0]["summary"] == "renamed first"
        assert stored_item.payload_json == stored_plan.items_json[0]
        assert stored_plan.updated_at.replace(tzinfo=timezone.utc) == updated_at
        assert stored_item.updated_at.replace(tzinfo=timezone.utc) == updated_at


def test_pending_update_plan_info_edit_updates_proposed_snapshot_by_item_id():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-EDIT")
    repository = CalendarMutationPlanRepository(database)
    previous = {
        "event_id": "event-1",
        "summary": "同名日程",
        "start_time": "2030-01-02T08:00:00+08:00",
        "end_time": "2030-01-02T09:00:00+08:00",
    }
    proposed = {**previous, "summary": "同名日程"}
    plan = repository.create(
        owner.id,
        operation="update",
        items=[
            {
                "event_id": "event-1",
                "previous": previous,
                "proposed": proposed,
                **proposed,
            },
            {
                "event_id": "event-2",
                "previous": {**previous, "event_id": "event-2"},
                "proposed": {**proposed, "event_id": "event-2"},
                **proposed,
                "event_id": "event-2",
            },
        ],
    )
    target_id = plan["ledger_items"][1]["id"]

    result = repository.update_pending_item(
        owner.id,
        plan["id"],
        target_id,
        summary="只修改第二项",
        start_time=datetime.fromisoformat("2030-01-02T10:30:00+08:00"),
        end_time=datetime.fromisoformat("2030-01-02T11:30:00+08:00"),
    )

    assert result["items"][0]["summary"] == "同名日程"
    assert result["items"][1]["summary"] == "只修改第二项"
    assert result["items"][1]["proposed"]["summary"] == "只修改第二项"
    assert result["ledger_items"][1]["payload"] == result["items"][1]


def test_pending_plan_time_update_rejects_delete_expired_and_queued_plans():
    database = memory_database()
    owner = participant(database, "PLAN-EDIT-STATES")
    repository = CalendarMutationPlanRepository(database)
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    times = {
        "start_time": datetime.fromisoformat("2030-01-02T10:00:00+08:00"),
        "end_time": datetime.fromisoformat("2030-01-02T11:00:00+08:00"),
    }
    delete_plan = repository.create(
        owner.id,
        operation="delete",
        items=[{"event_id": "a"}, {"event_id": "b"}],
        now=now,
    )
    assert repository.update_pending_item_time(
        owner.id,
        delete_plan["id"],
        delete_plan["ledger_items"][0]["id"],
        now=now,
        **times,
    )["update_status"] == "not_editable"

    expired = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": "a"}, {"summary": "b"}],
        ttl_minutes=1,
        now=now,
    )
    expired_result = repository.update_pending_item_time(
        owner.id,
        expired["id"],
        expired["ledger_items"][0]["id"],
        now=now + timedelta(minutes=2),
        **times,
    )
    assert expired_result["update_status"] == "expired"
    assert repository.get(expired["id"])["status"] == "expired"

    queued = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": "a"}, {"summary": "b"}],
        now=now,
    )
    repository.request_execution(owner.id, queued["id"], now=now)
    assert repository.update_pending_item_time(
        owner.id,
        queued["id"],
        queued["ledger_items"][0]["id"],
        now=now,
        **times,
    )["update_status"] == "not_editable"


def test_pending_plan_time_update_requires_aware_increasing_times():
    database = memory_database()
    owner = participant(database, "PLAN-EDIT-VALIDATION")
    repository = CalendarMutationPlanRepository(database)
    plan = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": "a"}, {"summary": "b"}],
    )
    item_id = plan["ledger_items"][0]["id"]

    with pytest.raises(ValueError, match="timezone"):
        repository.update_pending_item_time(
            owner.id,
            plan["id"],
            item_id,
            start_time=datetime(2030, 1, 2, 10),
            end_time=datetime(2030, 1, 2, 11),
        )
    with pytest.raises(ValueError, match="after start"):
        repository.update_pending_item_time(
            owner.id,
            plan["id"],
            item_id,
            start_time=datetime.fromisoformat("2030-01-02T11:00:00+08:00"),
            end_time=datetime.fromisoformat("2030-01-02T10:00:00+08:00"),
        )


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
    assert verifier.calls == []
    cards = outbox.take_cards(ctx.agent_run_id)
    assert len(cards) == 1
    confirm = _plan_action(cards[0], "_confirm")
    assert "events" not in confirm and "event_ids" not in confirm
    edit_actions = [
        element["behaviors"][0]["value"]
        for element in cards[0]["body"]["elements"]
        if element.get("tag") == "button"
        and element["behaviors"][0]["value"]["mindflow_action"].endswith(
            "_edit_open"
        )
    ]
    assert len(edit_actions) == 2
    assert all(
        set(action) == {"mindflow_action", "version", "plan_id", "item_id"}
        for action in edit_actions
    )

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
    assert verifier.calls == []
    card = outbox.take_cards(ctx.agent_run_id)[0]
    serialized = json.dumps(card, ensure_ascii=False)
    assert "weekend-1" not in serialized and "weekend-2" not in serialized
    assert "calendar_mutation_plan_item_time_open" not in serialized

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


def test_calendar_plan_time_actions_update_pending_plan_without_provider_write():
    database = memory_database()
    owner = participant(database, "PLAN-CARD-EDIT")
    calendar = _Calendar()
    verifier = _Verifier()
    plans, outbox, tools, registry, _runner = _stack(database, calendar, verifier)
    ctx = _context(owner.id, "添加两个日程")
    asyncio.run(
        registry.execute(
            ctx,
            "calendar_create_events_plan",
            {
                "events": [
                    {
                        "summary": "同日任务",
                        "start_time": "2030-01-12T13:43:12+08:00",
                        "end_time": "2030-01-12T14:17:59+08:00",
                    },
                    {
                        "summary": "跨日任务",
                        "start_time": "2030-01-13T23:00:00+08:00",
                        "end_time": "2030-01-14T01:00:00+08:00",
                    },
                ]
            },
        )
    )
    confirmation = outbox.take_cards(ctx.agent_run_id)[0]
    edit = next(
        element["behaviors"][0]["value"]
        for element in confirmation["body"]["elements"]
        if element.get("tag") == "button"
        and element["behaviors"][0]["value"]["mindflow_action"].endswith(
            "_edit_open"
        )
    )
    service = _card_service(database, tools)

    opened = service.handle(
        owner.id,
        message_id="plan-card",
        action_value=edit,
        form_value={},
    )
    assert opened["navigation_only"] is True
    form = opened["card"]["body"]["elements"][0]
    submit = next(
        element["behaviors"][0]["value"]
        for element in form["elements"]
        if element.get("tag") == "button"
    )
    result = service.handle(
        owner.id,
        message_id="plan-card",
        callback_event_id="edit-event-1",
        action_value=submit,
        form_value={
            "summary": "修改后的名称",
            "start_hour": "15",
            "start_minute": "11",
            "end_hour": "16",
            "end_minute": "22",
        },
    )

    assert result["ok"] is True
    assert calendar.created == []
    stored = plans.get(edit["plan_id"])
    assert stored["items"][0]["summary"] == "修改后的名称"
    assert stored["items"][0]["start_time"] == "2030-01-12T15:11:00+08:00"
    assert stored["items"][0]["end_time"] == "2030-01-12T16:22:00+08:00"
    assert stored["ledger_items"][0]["payload"] == stored["items"][0]
    rendered = json.dumps(result["card"], ensure_ascii=False)
    assert "2030-01-12 15:11–16:22" in rendered

    queued = service.handle(
        owner.id,
        message_id="plan-card",
        callback_event_id="confirm-after-edit",
        action_value=_plan_action(result["card"], "_confirm"),
        form_value={},
    )
    assert queued["status"] == "queued"
    asyncio.run(_runner.run_once())
    assert calendar.created[0][1]["start_time"].isoformat() == (
        "2030-01-12T15:11:00+08:00"
    )
    assert calendar.created[0][1]["end_time"].isoformat() == (
        "2030-01-12T16:22:00+08:00"
    )


def test_calendar_plan_time_edit_preserves_cross_day_dates_and_rejects_stale_card():
    database = memory_database()
    owner = participant(database, "PLAN-CARD-CROSS-DAY")
    repository = CalendarMutationPlanRepository(database)
    plan = repository.create(
        owner.id,
        operation="create",
        items=[
            {
                "summary": "cross day",
                "start_time": "2030-01-12T23:00:00+08:00",
                "end_time": "2030-01-13T01:00:00+08:00",
            },
            {
                "summary": "other",
                "start_time": "2030-01-14T10:00:00+08:00",
                "end_time": "2030-01-14T11:00:00+08:00",
            },
        ],
    )
    service = CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(
            on_observation_committed=lambda **_values: None
        ),
        calendar_mutation_plans=repository,
    )
    action = {
        "mindflow_action": "calendar_mutation_plan_item_time_submit",
        "version": "1",
        "plan_id": plan["id"],
        "item_id": plan["ledger_items"][0]["id"],
    }
    result = service.handle(
        owner.id,
        message_id="cross-day-card",
        action_value=action,
        form_value={
            "start_hour": "22",
            "start_minute": "30",
            "end_hour": "02",
            "end_minute": "15",
        },
    )
    updated = repository.get(plan["id"])

    assert result["ok"] is True
    assert updated["items"][0]["start_time"].startswith("2030-01-12T22:30:00")
    assert updated["items"][0]["end_time"].startswith("2030-01-13T02:15:00")

    repository.request_execution(owner.id, plan["id"])
    stale = service.handle(
        owner.id,
        message_id="cross-day-card",
        action_value=action,
        form_value={
            "start_hour": "20",
            "start_minute": "00",
            "end_hour": "21",
            "end_minute": "00",
        },
    )
    assert stale["error"] == "calendar_mutation_plan_not_editable"
    assert repository.get(plan["id"])["items"] == updated["items"]


def test_calendar_plan_time_edit_is_participant_bound_and_validates_range():
    database = memory_database()
    owner = participant(database, "PLAN-CARD-OWNER")
    other = participant(database, "PLAN-CARD-OTHER")
    repository = CalendarMutationPlanRepository(database)
    plan = repository.create(
        owner.id,
        operation="create",
        items=[
            {
                "summary": "a",
                "start_time": "2030-01-12T09:00:00+08:00",
                "end_time": "2030-01-12T10:00:00+08:00",
            },
            {
                "summary": "b",
                "start_time": "2030-01-12T11:00:00+08:00",
                "end_time": "2030-01-12T12:00:00+08:00",
            },
        ],
    )
    service = CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(
            on_observation_committed=lambda **_values: None
        ),
        calendar_mutation_plans=repository,
    )
    action = {
        "mindflow_action": "calendar_mutation_plan_item_time_submit",
        "version": "1",
        "plan_id": plan["id"],
        "item_id": plan["ledger_items"][0]["id"],
    }

    hidden = service.handle(
        other.id,
        message_id="foreign-card",
        action_value=action,
        form_value={},
    )
    invalid = service.handle(
        owner.id,
        message_id="owner-card",
        action_value=action,
        form_value={
            "start_hour": "11",
            "start_minute": "00",
            "end_hour": "10",
            "end_minute": "00",
        },
    )

    assert hidden["error"] == "calendar_mutation_plan_not_found"
    assert invalid["error"] == "invalid_calendar_plan_item_time"
    assert "结束时间必须晚于开始时间" in invalid["reply_text"]


@pytest.mark.parametrize("value", ["invalid-time", "2030-01-12T09:00:00"])
def test_update_plan_invalid_original_time_uses_generic_retry_copy(value):
    with pytest.raises(ValueError) as error:
        _aware_calendar_datetime(value)

    assert "重新发起日程操作" in str(error.value)
    assert "重新发起添加" not in str(error.value)


def test_calendar_plan_confirmation_fails_closed_on_ledger_mismatch():
    plan = {
        "id": str(uuid.uuid4()),
        "operation": "create",
        "items": [
            {
                "summary": "visible",
                "start_time": "2030-01-12T09:00:00+08:00",
                "end_time": "2030-01-12T10:00:00+08:00",
            },
            {
                "summary": "other",
                "start_time": "2030-01-12T11:00:00+08:00",
                "end_time": "2030-01-12T12:00:00+08:00",
            },
        ],
        "ledger_items": [{
            "id": str(uuid.uuid4()),
            "item_index": 0,
            "status": "pending",
            "payload": {"summary": "different"},
        }],
    }

    from app.integrations.feishu.cards import calendar_mutation_plan_confirmation_card

    with pytest.raises(ValueError, match="aggregate and ledger"):
        calendar_mutation_plan_confirmation_card(plan)


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
    asyncio.run(runner.run_once(now=now + timedelta(seconds=12)))

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

    asyncio.run(
            CalendarMutationPlanRunner(repository, execute).run_once(
                now=now + timedelta(seconds=12)
        )
    )

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
    assert intermediate["ledger_items"][0]["next_retry_at"] is not None

    assert asyncio.run(runner.run_once()) == 0
    asyncio.run(
        runner.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=6))
    )
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


def test_polite_question_with_concrete_create_action_stages_one_plan():
    database = memory_database()
    owner = participant(database, "PLAN-POLITE-CREATE")
    calendar = _Calendar()
    verifier = _Verifier()
    _plans, outbox, _tools, registry, _runner = _stack(
        database, calendar, verifier
    )
    ctx = _context(owner.id, "能不能帮我把这周六周天都加上？")

    result = asyncio.run(
        registry.execute(
            ctx,
            "calendar_create_events_plan",
            {
                "events": [
                    {
                        "summary": "周末安排",
                        "start_time": "2030-01-12T17:30:00+08:00",
                        "end_time": "2030-01-12T19:00:00+08:00",
                    },
                    {
                        "summary": "周末安排",
                        "start_time": "2030-01-13T17:30:00+08:00",
                        "end_time": "2030-01-13T19:00:00+08:00",
                    },
                ]
            },
        )
    )

    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert len(outbox.take_cards(ctx.agent_run_id)) == 1
    assert calendar.created == []


def test_single_create_stages_confirmation_plan_without_provider_write():
    database = memory_database()
    owner = participant(database, "PLAN-SINGLE-CREATE")
    calendar = _Calendar()
    _plans, outbox, _tools, registry, _runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "明天下午加一个组会")

    result = asyncio.run(registry.execute(
        ctx,
        "calendar_create_event",
        {
            "summary": "组会",
            "recurrence_mode": "single",
            "start_time": "2030-01-12T15:00:00+08:00",
            "end_time": "2030-01-12T16:00:00+08:00",
        },
    ))

    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert result.result["item_count"] == 1
    assert calendar.created == []
    assert len(outbox.take_cards(ctx.agent_run_id)) == 1


def test_recurring_create_plan_preserves_recurrence_until_runner_execution():
    database = memory_database()
    owner = participant(database, "PLAN-RECURRING-CREATE")
    calendar = _Calendar()
    plans, _outbox, _tools, registry, runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "每周一上午九点开组会，共四次")

    result = asyncio.run(registry.execute(
        ctx,
        "calendar_create_event",
        {
            "summary": "组会",
            "recurrence_mode": "recurring",
            "recurrence_frequency": "WEEKLY",
            "recurrence_weekdays": ["MO"],
            "recurrence_count": 4,
            "start_time": "2030-01-07T09:00:00+08:00",
            "end_time": "2030-01-07T10:00:00+08:00",
            "reminder_minutes": 15,
            "description": "保留说明",
        },
    ))
    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert calendar.created == []
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    stored = plans.get(plan_id)
    expected_rule = "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO;COUNT=4"
    assert stored["items"][0]["recurrence"] == expected_rule
    plans.request_execution(owner.id, plan_id)

    asyncio.run(runner.run_once())

    created = calendar.created[0][1]
    assert created["recurrence"] == expected_rule
    assert created["reminder_minutes"] == 15
    assert created["description"] == "保留说明"


def test_update_plan_applies_new_weekly_recurrence_after_confirmation():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-RECURRENCE")
    calendar = _Calendar()
    calendar.events["weekend-1"]["start_time"] = "2030-01-07T17:30:00+08:00"
    calendar.events["weekend-1"]["end_time"] = "2030-01-07T19:00:00+08:00"
    plans, _outbox, _tools, registry, runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "把周一活动改成每周一重复，共四次")
    expected_rule = "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO;COUNT=4"

    result = asyncio.run(registry.execute(
        ctx,
        "calendar_update_event",
        {
            "event_ref": "weekend-1",
            "changes": {
                "recurrence_frequency": "WEEKLY",
                "recurrence_weekdays": ["MO"],
                "recurrence_count": 4,
            },
        },
    ))

    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert calendar.updated == []
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    assert plans.get(plan_id)["items"][0]["recurrence"] == expected_rule
    plans.request_execution(owner.id, plan_id)
    asyncio.run(runner.run_once())

    assert calendar.updated[0][2]["recurrence"] == expected_rule


def test_batch_update_plan_applies_normalized_recurrence():
    database = memory_database()
    owner = participant(database, "PLAN-BATCH-UPDATE-RECURRENCE")
    calendar = _Calendar()
    calendar.events["weekend-1"]["start_time"] = "2030-01-07T17:30:00+08:00"
    calendar.events["weekend-1"]["end_time"] = "2030-01-07T19:00:00+08:00"
    plans, _outbox, _tools, registry, runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "把两个活动改为不同的重复规则")

    result = asyncio.run(registry.execute(
        ctx,
        "calendar_update_events_plan",
        {
            "updates": [
                {
                    "event_ref": "weekend-1",
                    "changes": {
                        "recurrence_frequency": "WEEKLY",
                        "recurrence_weekdays": ["MO"],
                        "recurrence_count": 4,
                    },
                },
                {
                    "event_ref": "weekend-2",
                    "changes": {
                        "recurrence_frequency": "DAILY",
                        "recurrence_count": 2,
                    },
                },
            ]
        },
    ))

    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert calendar.updated == []
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    plans.request_execution(owner.id, plan_id)
    asyncio.run(runner.run_once())

    assert [call[2]["recurrence"] for call in calendar.updated] == [
        "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO;COUNT=4",
        "FREQ=DAILY;INTERVAL=1;COUNT=2",
    ]


def test_update_plan_preserves_existing_recurrence_when_not_requested():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-KEEP-RECURRENCE")
    calendar = _Calendar()
    existing_rule = "FREQ=WEEKLY;INTERVAL=1;BYDAY=SA"
    calendar.events["weekend-1"]["recurrence"] = existing_rule
    plans, _outbox, _tools, registry, runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "只修改活动名称")

    asyncio.run(registry.execute(
        ctx,
        "calendar_update_event",
        {"event_ref": "weekend-1", "changes": {"summary": "新名称"}},
    ))
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    plans.request_execution(owner.id, plan_id)
    asyncio.run(runner.run_once())

    assert calendar.updated[0][2]["recurrence"] == existing_rule
    assert calendar.updated[0][2]["clear_recurrence"] is False


def test_calendar_patch_updates_one_time_field_and_preserves_omitted_fields():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-PATCH")
    calendar = _Calendar()
    calendar.events["weekend-1"]["description"] = "原说明"
    plans, _outbox, _tools, registry, _runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "只把结束时间改到晚上八点")

    result = asyncio.run(
        registry.execute(
            ctx,
            "calendar_update_event",
            {
                "event_ref": "weekend-1",
                "changes": {"end_time": "2030-01-12T20:00:00+08:00"},
            },
        )
    )

    assert result.result["calendar_mutation"] == "pending_confirmation"
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    proposed = plans.get(plan_id)["items"][0]["proposed"]
    assert proposed["start_time"] == "2030-01-12T17:30:00+08:00"
    assert proposed["end_time"] == "2030-01-12T20:00:00+08:00"
    assert proposed["summary"] == "军训服回收志愿服务"
    assert proposed["description"] == "原说明"


def test_calendar_patch_start_only_preserves_existing_end():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-PATCH-START")
    calendar = _Calendar()
    plans, _outbox, _tools, registry, _runner = _stack(
        database, calendar, _Verifier()
    )

    result = asyncio.run(
        registry.execute(
            _context(owner.id, "只把开始时间改到下午六点"),
            "calendar_update_event",
            {
                "event_ref": "weekend-1",
                "changes": {"start_time": "2030-01-12T18:00:00+08:00"},
            },
        )
    )

    assert result.result["calendar_mutation"] == "pending_confirmation"
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    proposed = plans.get(plan_id)["items"][0]["proposed"]
    assert proposed["start_time"] == "2030-01-12T18:00:00+08:00"
    assert proposed["end_time"] == "2030-01-12T19:00:00+08:00"


def test_update_plan_clear_recurrence_still_clears_rule():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-CLEAR-RECURRENCE")
    calendar = _Calendar()
    calendar.events["weekend-1"]["recurrence"] = (
        "FREQ=WEEKLY;INTERVAL=1;BYDAY=SA"
    )
    plans, _outbox, _tools, registry, runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "取消活动重复")

    asyncio.run(registry.execute(
        ctx,
        "calendar_update_event",
        {"event_ref": "weekend-1", "changes": {"clear_recurrence": True}},
    ))
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    plans.request_execution(owner.id, plan_id)
    asyncio.run(runner.run_once())

    update_args = calendar.updated[0][2]
    assert update_args["recurrence"] is None
    assert update_args["clear_recurrence"] is True


def test_update_plan_recurrence_change_waits_for_confirmation():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-RECURRENCE-WAIT")
    calendar = _Calendar()
    plans, _outbox, _tools, registry, runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "把活动改成每天重复两次")

    asyncio.run(registry.execute(
        ctx,
        "calendar_update_event",
        {
            "event_ref": "weekend-1",
            "changes": {
                "recurrence_frequency": "DAILY",
                "recurrence_count": 2,
            },
        },
    ))

    assert asyncio.run(runner.run_once()) == 0
    assert calendar.updated == []
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    plans.request_execution(owner.id, plan_id)
    assert asyncio.run(runner.run_once()) == 1
    assert calendar.updated[0][2]["recurrence"] == (
        "FREQ=DAILY;INTERVAL=1;COUNT=2"
    )


def test_single_update_stages_then_runner_executes_after_confirmation():
    database = memory_database()
    owner = participant(database, "PLAN-SINGLE-UPDATE")
    calendar = _Calendar()
    plans, outbox, tools, registry, runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "把周六的活动改名")

    result = asyncio.run(registry.execute(
        ctx,
        "calendar_update_event",
        {"event_ref": "weekend-1", "changes": {"summary": "项目讨论"}},
    ))

    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert calendar.updated == []
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    plan = plans.get(plan_id)
    card = outbox.take_cards(ctx.agent_run_id)[0]
    confirmation = _plan_action(card, "_confirm")
    handled = _card_service(database, tools).handle(
        owner.id,
        message_id="update-plan-card",
        callback_event_id="confirm-update",
        action_value=confirmation,
        form_value={},
    )
    assert handled["status"] == "queued"
    asyncio.run(runner.run_once())

    assert len(calendar.updated) == 1
    assert calendar.updated[0][2]["summary"] == "项目讨论"
    assert plans.get(plan["id"])["status"] == "succeeded"


def test_single_delete_uses_generic_plan_and_waits_for_confirmation():
    database = memory_database()
    owner = participant(database, "PLAN-SINGLE-DELETE")
    calendar = _Calendar()
    plans, outbox, _tools, registry, _runner = _stack(
        database, calendar, _Verifier()
    )
    ctx = _context(owner.id, "删除周六的志愿服务")

    result = asyncio.run(registry.execute(
        ctx,
        "calendar_delete_event",
        {"event_id": "weekend-1"},
    ))

    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert calendar.deleted == []
    with database.session() as session:
        plan_id = str(session.scalars(select(CalendarMutationPlan)).one().id)
    assert plans.get(plan_id)["operation"] == "delete"
    serialized = json.dumps(outbox.take_cards(ctx.agent_run_id)[0])
    assert "calendar_mutation_plan_confirm" in serialized
    assert "calendar_delete_confirm" not in serialized


def test_update_outcome_unknown_reads_back_and_never_blindly_retries_divergent_state():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-UNKNOWN")
    calendar = _Calendar()
    plans, _outbox, tools, _registry, _runner = _stack(
        database, calendar, _Verifier()
    )
    previous = dict(calendar.events["weekend-1"])
    proposed = {**previous, "summary": "项目讨论"}
    plan = plans.create(
        owner.id,
        operation="update",
        items=[{
            "event_id": "weekend-1",
            "previous": previous,
            "proposed": proposed,
            "summary": "项目讨论",
            "start_time": proposed["start_time"],
            "end_time": proposed["end_time"],
        }],
    )
    calendar.events["weekend-1"]["summary"] = "第三方已改名"
    item = {**plan["ledger_items"][0], "reconcile": True}

    with pytest.raises(CalendarMutationOutcomeUnknown):
        asyncio.run(tools.execute_calendar_mutation_plan_item(plan, item))

    assert calendar.updated == []


def test_runner_completion_uses_modified_verb():
    database = memory_database()
    owner = participant(database, "PLAN-UPDATE-VERB")
    repository = CalendarMutationPlanRepository(database)
    plan = repository.create(
        owner.id,
        operation="update",
        items=[{"event_id": "one", "summary": "updated"}],
    )
    repository.request_execution(
        owner.id,
        plan["id"],
        status_card_message_id="om-update",
        status_card_chat_id="oc-update",
    )

    class Sender:
        def __init__(self):
            self.cards = []

        def update_card(self, message_id, card):
            self.cards.append((message_id, card))

    sender = Sender()

    async def execute(_plan, _item):
        return {"ok": True, "updated": {"id": "one"}}

    asyncio.run(
        CalendarMutationPlanRunner(repository, execute, sender=sender).run_once()
    )

    visible = json.dumps(sender.cards[0][1], ensure_ascii=False)
    assert "已修改 1 个日程" in visible
    assert "已删除" not in visible


def test_polite_question_with_concrete_delete_action_stages_one_plan():
    database = memory_database()
    owner = participant(database, "PLAN-POLITE-DELETE")
    calendar = _Calendar()
    verifier = _Verifier(
        MutationIntentDecision(
            "allow", "destructive_action", "explicit_batch_delete"
        )
    )
    _plans, outbox, _tools, registry, _runner = _stack(
        database, calendar, verifier
    )
    ctx = _context(owner.id, "删除刚刚加入的两个周末事件，行不行？")

    result = asyncio.run(
        registry.execute(
            ctx,
            "calendar_delete_events_plan",
            {"event_ids": ["weekend-1", "weekend-2"]},
        )
    )

    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert len(outbox.take_cards(ctx.agent_run_id)) == 1
    assert calendar.deleted == []


def test_calendar_stage_does_not_call_mutation_verifier_or_provider():
    database = memory_database()
    owner = participant(database, "PLAN-CAPABILITY")
    calendar = _Calendar()
    verifier = _Verifier(
        MutationIntentDecision(
            "deny", "capability_question", "capability_question"
        )
    )
    _plans, outbox, _tools, registry, _runner = _stack(
        database, calendar, verifier
    )
    ctx = _context(owner.id, "你能不能一次删除两个日程？")

    result = asyncio.run(
        registry.execute(
            ctx,
            "calendar_delete_events_plan",
            {"event_ids": ["weekend-1", "weekend-2"]},
        )
    )

    with database.session() as session:
        plan_count = session.scalar(
            select(func.count()).select_from(CalendarMutationPlan)
        )
    assert result.result["calendar_mutation"] == "pending_confirmation"
    assert verifier.calls == []
    assert plan_count == 1
    assert len(outbox.take_cards(ctx.agent_run_id)) == 1
    assert calendar.deleted == []


def test_terminal_plan_presentation_failure_is_retried_after_runner_restart():
    database = memory_database()
    owner = participant(database, "PLAN-PRESENTATION-RECOVERY")
    repository = CalendarMutationPlanRepository(database)
    now = datetime.now(timezone.utc)
    plan = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": "first"}, {"summary": "second"}],
        now=now,
    )
    repository.request_execution(
        owner.id,
        plan["id"],
        status_card_message_id="om-confirmation",
        status_card_chat_id="oc-chat",
        now=now,
    )

    class RetryableSender:
        def __init__(self, *, fail=False):
            self.fail = fail
            self.updates = []
            self.sends = []

        def update_card(self, message_id, card):
            self.updates.append((message_id, card))
            if self.fail:
                raise FeishuSendError(
                    "temporary update failure",
                    retryable=True,
                    operation="update_card",
                )

        def send_card(self, chat_id, card, *, message_uuid=None):
            self.sends.append((chat_id, card, message_uuid))
            return "om-result"

    failing_sender = RetryableSender(fail=True)

    async def execute(_plan, item):
        return {"ok": True, "created": {"id": f"event-{item['item_index']}"}}

    first_runner = CalendarMutationPlanRunner(
        repository, execute, sender=failing_sender
    )
    asyncio.run(first_runner.run_once())
    failed_presentation = repository.get(plan["id"])
    assert failed_presentation["status"] == "succeeded"
    assert failed_presentation["completion_presented_at"] is None
    assert failed_presentation["completion_presentation_attempts"] == 1
    assert failed_presentation["completion_presentation_error"] == "FeishuSendError"

    recovered_sender = RetryableSender()
    second_runner = CalendarMutationPlanRunner(
        repository, execute, sender=recovered_sender
    )
    assert asyncio.run(second_runner.run_once(now=datetime.now(timezone.utc))) == 0
    asyncio.run(
        second_runner.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=6))
    )
    recovered = repository.get(plan["id"])
    assert recovered["completion_presented_at"] is not None
    assert recovered["completion_presentation_attempts"] == 2
    assert recovered["completion_presentation_error"] is None
    assert recovered_sender.updates[0][0] == "om-confirmation"


def test_completion_presentation_retries_use_backoff_and_report_once():
    database = memory_database()
    owner = participant(database, "PLAN-PRESENTATION-BACKOFF")
    repository = CalendarMutationPlanRepository(database)
    base = datetime.now(timezone.utc)
    plan = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": "first"}, {"summary": "second"}],
        now=base,
    )
    repository.request_execution(
        owner.id,
        plan["id"],
        status_card_message_id="om-stuck",
        status_card_chat_id="oc-chat",
        now=base,
    )

    class BrokenSender:
        def update_card(self, message_id, card):
            raise FeishuSendError(
                "card permanently gone",
                retryable=True,
                operation="update_card",
            )

    async def execute(_plan, item):
        return {"ok": True, "created": {"id": f"event-{item['item_index']}"}}

    runner = CalendarMutationPlanRunner(repository, execute, sender=BrokenSender())
    asyncio.run(runner.run_once())
    assert repository.get(plan["id"])["completion_presentation_attempts"] == 1

    # A failure only becomes retryable after the backoff for its attempt count.
    assert asyncio.run(runner.run_once(now=base + timedelta(seconds=1))) == 0

    for offset in (6, 21, 81, 381, 1281):
        asyncio.run(runner.run_once(now=base + timedelta(seconds=offset)))
    final = repository.get(plan["id"])
    assert final["status"] == "succeeded"
    assert final["completion_presented_at"] is None
    assert final["completion_presentation_attempts"] == 6
    with database.session() as session:
        incidents = list(
            session.scalars(
                select(RuntimeIncident).where(
                    RuntimeIncident.event_name
                    == "completion_presentation_retry_exhausted"
                )
            )
        )
    assert len(incidents) == 1
    assert incidents[0].details_json["plan_id"] == plan["id"]
    assert incidents[0].severity == "error"


def test_outcome_unknown_incident_is_reported_once_per_item():
    database = memory_database()
    owner = participant(database, "PLAN-UNKNOWN-INCIDENT")
    repository = CalendarMutationPlanRepository(database)
    base = datetime.now(timezone.utc)
    plan = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": "first"}, {"summary": "second"}],
        now=base,
    )
    repository.request_execution(owner.id, plan["id"], now=base)

    async def execute(_plan, item):
        raise CalendarMutationOutcomeUnknown(
            "provider result ambiguous", request_kind="create_event"
        )

    runner = CalendarMutationPlanRunner(repository, execute)
    asyncio.run(runner.run_once())

    now = base + timedelta(seconds=6)
    for _ in range(5):
        asyncio.run(runner.run_once(now=now))
        now += timedelta(seconds=960)

    final = repository.get(plan["id"])
    assert final["status"] == "recovery_required"
    assert final["ledger_items"][0]["attempt_count"] == 6
    with database.session() as session:
        incidents = list(
            session.scalars(
                select(RuntimeIncident).where(
                    RuntimeIncident.event_name == "outcome_unknown_retry_exhausted"
                )
            )
        )
    assert len(incidents) == 1
    assert incidents[0].details_json["plan_id"] == plan["id"]
    assert incidents[0].details_json["attempt_count"] == 5


def test_provider_unavailable_is_distinct_from_unknown_and_uses_backoff():
    database = memory_database()
    owner = participant(database, "PLAN-PROVIDER-UNAVAILABLE")
    repository = CalendarMutationPlanRepository(database)
    base = datetime.now(timezone.utc)
    plan = repository.create(
        owner.id,
        operation="create",
        items=[{"summary": "first"}, {"summary": "second"}],
        now=base,
    )
    repository.request_execution(owner.id, plan["id"], now=base)
    attempts = []

    async def execute(_plan, item):
        attempts.append(item["item_index"])
        if len(attempts) == 1:
            raise CalendarProviderUnavailable(
                "calendar read unavailable", request_kind="calendar_preflight"
            )
        return {"ok": True, "created": {"id": f"event-{item['item_index']}"}}

    runner = CalendarMutationPlanRunner(repository, execute)
    asyncio.run(runner.run_once())
    interim = repository.get(plan["id"])
    assert CalendarMutationPlanRunner._outcome_unknown(
        CalendarProviderUnavailable("unavailable", request_kind="read")
    ) is False
    assert interim["status"] == "recovery_required"
    assert interim["ledger_items"][0]["status"] == "failed"
    assert interim["ledger_items"][0]["error_code"] == "calendar_provider_unavailable"
    assert asyncio.run(runner.run_once()) == 0

    asyncio.run(
        runner.run_once(now=base + timedelta(seconds=6))
    )
    assert repository.get(plan["id"])["status"] == "succeeded"
    assert attempts == [0, 0, 1]
