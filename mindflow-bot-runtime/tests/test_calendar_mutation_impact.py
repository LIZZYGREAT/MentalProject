import asyncio
from datetime import date, datetime, time, timedelta
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.agent.context import AgentContext
from app.repositories import (
    ForecastSnapshotRepository,
    ParticipantRepository,
)
from app.services.calendar_mutation_impact import CalendarMutationImpactResolver
from app.services.forecast_mutation_refresh import ForecastMutationRefreshQueue
from app.tools.care import CareTools
from helpers import memory_database, warning_repository


TZ = ZoneInfo("Asia/Shanghai")


def _event(start: datetime, end: datetime, recurrence: str = ""):
    return {
        "id": "calendar-event",
        "summary": "重复课程",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "recurrence": recurrence,
    }


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            datetime(2026, 8, 26, 9, 0, tzinfo=TZ),
            datetime(2026, 8, 28, 18, 0, tzinfo=TZ),
            {date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)},
        ),
        (
            datetime(2026, 8, 26, 23, 0, tzinfo=TZ),
            datetime(2026, 8, 27, 1, 0, tzinfo=TZ),
            {date(2026, 8, 26), date(2026, 8, 27)},
        ),
        (
            datetime(2026, 8, 26, 9, 0, tzinfo=TZ),
            datetime(2026, 8, 27, 0, 0, tzinfo=TZ),
            {date(2026, 8, 26)},
        ),
        (
            datetime(2026, 8, 26, 9, 0, tzinfo=TZ),
            datetime(2026, 8, 26, 10, 0, tzinfo=TZ),
            {date(2026, 8, 26)},
        ),
    ],
)
def test_calendar_event_dates_follow_half_open_interval(start, end, expected):
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")

    assert resolver.covered_dates(_event(start, end)) == expected


def test_recurrence_matches_only_persisted_affected_dates():
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")
    monday = datetime(2026, 8, 24, 9, 0, tzinfo=TZ)
    persisted = {
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 26),
        date(2026, 8, 31),
        date(2026, 9, 7),
        date(2026, 9, 8),
    }

    daily = resolver.affected_dates(
        previous=None,
        updated=_event(
            monday,
            monday + timedelta(hours=1),
            "FREQ=DAILY;INTERVAL=1;COUNT=3",
        ),
        persisted_dates=persisted,
    )
    weekly = resolver.affected_dates(
        previous=None,
        updated=_event(
            monday,
            monday + timedelta(hours=1),
            "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO",
        ),
        persisted_dates=persisted,
    )

    assert daily == {
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 26),
    }
    assert weekly == {
        date(2026, 8, 24),
        date(2026, 8, 31),
        date(2026, 9, 7),
    }
    assert date(2026, 9, 8) not in weekly


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        (
            "FREQ=MONTHLY;INTERVAL=2;COUNT=3",
            {date(2026, 1, 15), date(2026, 3, 15), date(2026, 5, 15)},
        ),
        (
            "FREQ=YEARLY;INTERVAL=1;COUNT=2",
            {date(2026, 1, 15), date(2027, 1, 15)},
        ),
        (
            "FREQ=DAILY;INTERVAL=1;UNTIL=20260117T010000Z",
            {date(2026, 1, 15), date(2026, 1, 16), date(2026, 1, 17)},
        ),
    ],
)
def test_supported_recurrence_subset_remains_precise(rule, expected):
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")
    start = datetime(2026, 1, 15, 9, 0, tzinfo=TZ)
    persisted = {
        date(2026, 1, 15),
        date(2026, 1, 16),
        date(2026, 1, 17),
        date(2026, 1, 18),
        date(2026, 2, 15),
        date(2026, 3, 15),
        date(2026, 5, 15),
        date(2027, 1, 15),
        date(2028, 1, 15),
    }

    affected = resolver.affected_dates(
        previous=None,
        updated=_event(start, start + timedelta(hours=1), rule),
        persisted_dates=persisted,
    )

    assert affected == expected


def test_recurrence_update_and_clear_use_old_union_new_persisted_dates():
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")
    start = datetime(2026, 8, 24, 9, 0, tzinfo=TZ)
    old = _event(
        start,
        start + timedelta(hours=1),
        "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO",
    )
    updated = _event(
        start + timedelta(days=1),
        start + timedelta(days=1, hours=1),
        "FREQ=WEEKLY;INTERVAL=1;BYDAY=TU",
    )
    persisted = {
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 9, 2),
    }

    changed = resolver.affected_dates(
        previous=old,
        updated=updated,
        persisted_dates=persisted,
    )
    cleared = resolver.affected_dates(
        previous=old,
        updated=updated,
        persisted_dates=persisted,
        clear_recurrence=True,
    )

    assert changed == {
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 31),
        date(2026, 9, 1),
    }
    assert cleared == {
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 31),
    }


@pytest.mark.parametrize("selector", ["BYMONTHDAY=15", "BYMONTH=9", "BYSETPOS=1"])
def test_unsupported_recurrence_selectors_conservatively_match_persisted_future(
    selector,
):
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")
    start = datetime(2026, 8, 1, 9, 0, tzinfo=TZ)
    persisted = {
        date(2026, 7, 31),
        date(2026, 8, 1),
        date(2026, 8, 15),
        date(2026, 9, 1),
        date(2026, 9, 15),
    }

    affected = resolver.affected_dates(
        previous=_event(
            start,
            start + timedelta(hours=1),
            f"FREQ=MONTHLY;INTERVAL=1;{selector}",
        ),
        updated=None,
        persisted_dates=persisted,
    )

    # Unknown provider semantics are never reinterpreted as DTSTART.day.
    # The conservative set remains bounded to already-persisted future dates.
    assert affected == persisted - {date(2026, 7, 31)}


def test_exact_31_day_recurring_duration_matches_final_overlap_date():
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")
    start = datetime(2026, 8, 1, 9, 0, tzinfo=TZ)
    event = _event(
        start,
        start + timedelta(days=31),
        "FREQ=YEARLY;INTERVAL=1;COUNT=2",
    )

    affected = resolver.affected_dates(
        previous=event,
        updated=None,
        persisted_dates={date(2027, 9, 1), date(2027, 9, 2)},
    )

    assert date(2027, 9, 1) in affected
    assert date(2027, 9, 2) not in affected


def _save_forecast(repository, participant_id, target):
    return repository.save(
        participant_id,
        target,
        calendar_revision=f"calendar-{target}",
        semantic_revision="semantic",
        algorithm_version="algorithm",
        forecast_version=f"forecast-{target}",
        semantic_status="rules_only",
        semantic_input=[],
        curve=[],
        peaks=[],
        warning_windows=[],
        output={},
    )


def test_recurring_calendar_create_invalidates_real_persisted_forecasts():
    database = memory_database()
    participant = ParticipantRepository(database).create("CALENDAR-RECURRENCE")
    forecasts = ForecastSnapshotRepository(database)
    warnings = warning_repository(database)
    today = datetime.now(TZ).date()
    affected = {today + timedelta(days=index) for index in range(3)}
    unrelated = today + timedelta(days=3)
    for target in affected | {unrelated}:
        _save_forecast(forecasts, participant.id, target)

    start = datetime.combine(today, time(9, 0), TZ)
    end = start + timedelta(hours=1)

    class Calendar:
        async def create_event(self, *_args, **_kwargs):
            return _event(
                start,
                end,
                "FREQ=DAILY;INTERVAL=1;COUNT=3",
            )

    class Coordinator:
        def __init__(self):
            self.forecasts = forecasts
            self.warnings = warnings
            self.dependency_refresh = None

        async def ensure_forecast(self, *_args, **_kwargs):
            await asyncio.Event().wait()

        def mark_dependency_dirty(self, participant_id, target, *, reason):
            return forecasts.invalidate_current_for_date(
                warnings, participant_id, target, reason=reason
            )

    coordinator = Coordinator()
    mutation_refresh = ForecastMutationRefreshQueue(coordinator)
    tools = CareTools(
        None, None, Calendar(), None, "Asia/Shanghai", coordinator, forecasts,
        mutation_refresh=mutation_refresh,
    )
    ctx = AgentContext(
        participant.id, "P", "ou", "oc", "calendar-create", uuid.uuid4()
    )

    async def scenario():
        mutation_refresh.start()
        result = await tools.create_calendar_event(
            ctx,
            {
                "summary": "重复课程",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "recurrence_frequency": "DAILY",
                "recurrence_count": 3,
            },
        )
        await mutation_refresh.close()
        return result

    result = asyncio.run(scenario())

    assert result["forecast_refresh"] == "queued"
    assert set(result["forecast_refresh_queued_dates"]) == {
        target.isoformat() for target in affected
    }
    assert result["forecast_refresh_errors"] == []
    assert all(forecasts.latest(participant.id, target) is None for target in affected)
    assert forecasts.latest(participant.id, unrelated) is not None


def test_large_recurring_mutation_invalidates_all_before_return_without_waiting_refresh():
    database = memory_database()
    participant = ParticipantRepository(database).create("CALENDAR-LARGE-RECURRENCE")
    forecasts = ForecastSnapshotRepository(database)
    warnings = warning_repository(database)
    today = datetime.now(TZ).date()
    affected = [today + timedelta(days=index) for index in range(20)]
    unrelated = today + timedelta(days=20)
    for target in [*affected, unrelated]:
        _save_forecast(forecasts, participant.id, target)

    start = datetime.combine(today, time(9, 0), TZ)
    end = start + timedelta(hours=1)

    class Calendar:
        async def create_event(self, *_args, **_kwargs):
            return _event(
                start,
                end,
                "FREQ=DAILY;INTERVAL=1;COUNT=20",
            )

    class Coordinator:
        def __init__(self):
            self.forecasts = forecasts
            self.warnings = warnings
            self.dependency_refresh = None
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = []

        async def ensure_forecast(self, _participant_id, target, *_args, **_kwargs):
            self.calls.append(target)
            self.started.set()
            await self.release.wait()
            return {"local_date": target.isoformat()}

    coordinator = Coordinator()
    mutation_refresh = ForecastMutationRefreshQueue(coordinator)
    tools = CareTools(
        None, None, Calendar(), None, "Asia/Shanghai", coordinator, forecasts,
        mutation_refresh=mutation_refresh,
    )
    ctx = AgentContext(
        participant.id, "P", "ou", "oc", "calendar-large", uuid.uuid4()
    )

    async def scenario():
        mutation_refresh.start()
        heartbeat = asyncio.Event()

        async def pulse():
            await asyncio.sleep(0)
            heartbeat.set()

        pulse_task = asyncio.create_task(pulse())
        result = await asyncio.wait_for(
            tools.create_calendar_event(
                ctx,
                {
                    "summary": "长期重复课程",
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "recurrence_frequency": "DAILY",
                    "recurrence_count": 20,
                },
            ),
            timeout=1.0,
        )
        await pulse_task
        assert heartbeat.is_set()
        assert all(
            forecasts.latest(participant.id, target) is None
            for target in affected
        )
        assert forecasts.latest(participant.id, unrelated) is not None
        await asyncio.wait_for(coordinator.started.wait(), timeout=0.5)
        # Dates for one participant remain ordered and serial; the request did
        # not wait for even the first deliberately blocked rebuild.
        assert coordinator.calls == [affected[0]]
        coordinator.release.set()
        await asyncio.wait_for(mutation_refresh.wait_idle(), timeout=1.0)
        await mutation_refresh.close()
        return result

    result = asyncio.run(scenario())

    assert result["forecast_refresh"] == "queued"
    assert result["forecast_refreshed_dates"] == []
    assert result["forecast_refresh_errors"] == []
    assert coordinator.calls == affected


@pytest.mark.parametrize("partial_response", [True, False])
def test_recurrence_only_update_invalidates_old_and_new_dates_with_provider_response(
    partial_response,
):
    database = memory_database()
    participant = ParticipantRepository(database).create("CALENDAR-PARTIAL-PATCH")
    forecasts = ForecastSnapshotRepository(database)
    warnings = warning_repository(database)
    today = datetime.now(TZ).date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7)
    if monday < today:
        monday += timedelta(days=7)
    tuesday = monday + timedelta(days=1)
    persisted = {
        monday,
        tuesday,
        monday + timedelta(days=7),
        tuesday + timedelta(days=7),
    }
    unrelated = monday + timedelta(days=2)
    for target in persisted | {unrelated}:
        _save_forecast(forecasts, participant.id, target)

    start = datetime.combine(monday, time(9, 0), TZ)
    end = start + timedelta(hours=1)
    previous = _event(
        start, end, "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO"
    )
    new_rule = "FREQ=WEEKLY;INTERVAL=1;BYDAY=TU"

    class Calendar:
        async def get_event(self, *_args):
            return dict(previous)

        async def update_event(self, *_args, **_kwargs):
            if partial_response:
                return {
                    "id": "calendar-event",
                    "start_time": None,
                    "end_time": None,
                    "recurrence": new_rule,
                }
            return _event(start, end, new_rule)

    class Coordinator:
        def __init__(self):
            self.forecasts = forecasts
            self.warnings = warnings
            self.dependency_refresh = None

    class Queue:
        def __init__(self):
            self.targets = None

        def enqueue(self, _participant_id, dates, **_kwargs):
            self.targets = dict(dates)
            return True

    queue = Queue()
    tools = CareTools(
        None, None, Calendar(), None, "Asia/Shanghai", Coordinator(), forecasts,
        mutation_refresh=queue,
    )
    ctx = AgentContext(
        participant.id, "P", "ou", "oc", "partial-patch", uuid.uuid4()
    )

    result = asyncio.run(
        tools.update_calendar_event(
            ctx,
            {
                "event_id": "calendar-event",
                "recurrence_frequency": "WEEKLY",
                "recurrence_weekdays": ["TU"],
            },
        )
    )

    assert result["forecast_invalidation"] == "succeeded"
    assert set(queue.targets) == persisted
    assert all(forecasts.latest(participant.id, target) is None for target in persisted)
    assert forecasts.latest(participant.id, unrelated) is not None


def test_clear_recurrence_with_partial_response_invalidates_old_occurrences_only():
    database = memory_database()
    participant = ParticipantRepository(database).create("CALENDAR-PARTIAL-CLEAR")
    forecasts = ForecastSnapshotRepository(database)
    warnings = warning_repository(database)
    today = datetime.now(TZ).date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7)
    tuesday = monday + timedelta(days=1)
    old_dates = {monday, monday + timedelta(days=7)}
    unrelated = tuesday + timedelta(days=7)
    for target in old_dates | {unrelated}:
        _save_forecast(forecasts, participant.id, target)
    start = datetime.combine(monday, time(9, 0), TZ)
    previous = _event(
        start,
        start + timedelta(hours=1),
        "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO",
    )

    class Calendar:
        async def get_event(self, *_args):
            return dict(previous)

        async def update_event(self, *_args, **_kwargs):
            return {
                "id": "calendar-event",
                "start_time": None,
                "end_time": None,
                "recurrence": "",
            }

    class Coordinator:
        def __init__(self):
            self.forecasts = forecasts
            self.warnings = warnings
            self.dependency_refresh = None

    class Queue:
        def enqueue(self, *_args, **_kwargs):
            return True

    tools = CareTools(
        None, None, Calendar(), None, "Asia/Shanghai", Coordinator(), forecasts,
        mutation_refresh=Queue(),
    )
    ctx = AgentContext(
        participant.id, "P", "ou", "oc", "partial-clear", uuid.uuid4()
    )

    asyncio.run(
        tools.update_calendar_event(
            ctx,
            {"event_id": "calendar-event", "clear_recurrence": True},
        )
    )

    assert all(forecasts.latest(participant.id, target) is None for target in old_dates)
    assert forecasts.latest(participant.id, unrelated) is not None
