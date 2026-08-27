import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.models import ForecastSnapshot
from app.repositories import ForecastSnapshotRepository, ParticipantRepository
from app.repositories_daily_review import (
    DailyReviewResponseRepository,
    DailyReviewRevisionConflict,
    DailyReviewScheduleRepository,
)
from app.services.daily_review_scheduler import DailyReviewScheduler
from tests.helpers import memory_database, participant


def _seed_forecast(database, participant_id, target):
    with database.session() as session:
        session.add(
            ForecastSnapshot(
                participant_id=participant_id,
                local_date=target,
                calendar_revision="calendar",
                semantic_revision="semantic",
                observation_revision="observation",
                algorithm_version="algorithm",
                forecast_version="forecast-clock",
                semantic_status="rules_only",
                semantic_input_json=[],
                curve_json=[],
                peaks_json=[],
                warning_windows_json=[],
                output_json={},
                valid=True,
            )
        )


class FakeClock:
    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _scheduler(database, person, clock, sender, *, retry_base_seconds=60):
    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "clock-chat"}

    return DailyReviewScheduler(
        schedules=DailyReviewScheduleRepository(database),
        participants=ParticipantRepository(database),
        bindings=Bindings(),
        forecasts=ForecastSnapshotRepository(database),
        sender=sender,
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        retry_base_seconds=retry_base_seconds,
        clock=clock,
    )


def test_delivery_clock_records_send_completion_after_authorization():
    database = memory_database()
    person = participant(database, "DR-CLOCK-SENT")
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    _seed_forecast(database, person.id, due.date())
    clock = FakeClock(due)

    class Sender:
        def send_card(self, *_args, **_kwargs):
            clock.advance(seconds=6)
            return "message-at-completion"

    scheduler = _scheduler(database, person, clock, Sender())
    result = asyncio.run(scheduler.run_once())
    schedules = DailyReviewScheduleRepository(database)
    row = schedules.get(
        schedules.ensure(person.id, due.date(), due)["id"]
    )

    assert result["sent"] == 1
    assert _utc(row["authorized_at"]) == due
    assert _utc(row["sent_at"]) == due + timedelta(seconds=6)
    assert row["sent_at"] >= row["authorized_at"]


def test_failure_retry_is_based_on_failure_completion_time():
    database = memory_database()
    person = participant(database, "DR-CLOCK-FAILED")
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    _seed_forecast(database, person.id, due.date())
    clock = FakeClock(due)

    class Sender:
        def send_card(self, *_args, **_kwargs):
            clock.advance(seconds=8)
            raise TimeoutError("provider completed with timeout")

    scheduler = _scheduler(
        database, person, clock, Sender(), retry_base_seconds=60
    )
    result = asyncio.run(scheduler.run_once())
    repo = DailyReviewScheduleRepository(database)
    row = repo.ensure(person.id, due.date(), due)

    assert result["failed"] == 1
    assert _utc(row["updated_at"]) == due + timedelta(seconds=8)
    assert _utc(row["next_attempt_at"]) == due + timedelta(seconds=68)


def test_retry_that_reaches_valid_until_expires_immediately():
    database = memory_database()
    person = participant(database, "DR-RETRY-WINDOW")
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    repo = DailyReviewScheduleRepository(database)
    schedule = repo.ensure(
        person.id, due.date(), due, valid_until=due + timedelta(seconds=30)
    )
    claim = repo.claim_due(due, 120)[0]

    assert repo.mark_failed(
        schedule["id"],
        claim["claim_token"],
        now=due + timedelta(seconds=10),
        error=TimeoutError(),
        max_attempts=5,
        retry_base_seconds=20,
    )
    stored = repo.get(schedule["id"])
    assert stored["status"] == "expired"
    assert stored["next_attempt_at"] is None


def test_ensure_updates_only_an_existing_unsent_schedule_window():
    database = memory_database()
    person = participant(database, "DR-SCHEDULE-RECONFIGURE")
    first = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    repo = DailyReviewScheduleRepository(database)
    schedule = repo.ensure(
        person.id, first.date(), first, valid_until=first + timedelta(hours=2)
    )
    moved = repo.ensure(
        person.id,
        first.date(),
        first + timedelta(hours=1),
        valid_until=first + timedelta(hours=4),
    )
    assert moved["id"] == schedule["id"]
    assert _utc(moved["scheduled_at"]) == first + timedelta(hours=1)
    assert _utc(moved["valid_until"]) == first + timedelta(hours=4)


class _EmptyResult:
    def scalar_one_or_none(self):
        return None


class _AlwaysRevisionConflictSession:
    def __init__(self, owner):
        self.owner = owner

    def execute(self, _statement):
        return _EmptyResult()

    def scalar(self, _statement):
        return 0

    def add(self, _row):
        pass

    def flush(self):
        self.owner.flush_count += 1
        raise IntegrityError(
            "insert",
            {},
            Exception(
                "UNIQUE constraint failed: "
                "daily_review_responses.participant_id, "
                "daily_review_responses.local_date, "
                "daily_review_responses.revision"
            ),
        )


class _AlwaysRevisionConflictDatabase:
    def __init__(self):
        self.flush_count = 0

    @contextmanager
    def session(self):
        yield _AlwaysRevisionConflictSession(self)


def test_revision_conflicts_are_bounded_and_never_recurse():
    database = _AlwaysRevisionConflictDatabase()
    repo = DailyReviewResponseRepository(database)

    with pytest.raises(DailyReviewRevisionConflict):
        repo.add(
            uuid.uuid4(),
            date(2030, 1, 15),
            callback_event_id="bounded-callback",
            submitted_at=datetime(2030, 1, 15, tzinfo=timezone.utc),
            card_version="daily-review-v1",
            schedule_id=None,
            causal_source_forecast_id=uuid.uuid4(),
            causal_source_forecast_version="forecast",
            values={
                "start_stress": 1.0,
                "start_energy": 1.0,
                "peak_stress": 1.0,
                "peak_period": "morning",
                "end_stress": 1.0,
                "end_energy": 1.0,
                "energy_consumption": 1.0,
                "main_stressor": None,
                "recovery_note": None,
                "free_text": None,
            },
            raw={},
        )
    assert database.flush_count == repo.MAX_REVISION_RETRIES


def test_unexpected_foreign_key_integrity_error_is_exposed():
    database = memory_database()
    event.listen(database.engine, "connect", lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"))
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    person = participant(database, "DR-UNEXPECTED-INTEGRITY")
    repo = DailyReviewResponseRepository(database)

    with pytest.raises(IntegrityError) as captured:
        repo.add(
            person.id,
            date(2030, 1, 15),
            callback_event_id="unexpected-fk",
            submitted_at=datetime(2030, 1, 15, tzinfo=timezone.utc),
            card_version="daily-review-v1",
            schedule_id=None,
            causal_source_forecast_id=uuid.uuid4(),
            causal_source_forecast_version="missing",
            values={
                "start_stress": 1.0,
                "start_energy": 1.0,
                "peak_stress": 1.0,
                "peak_period": "morning",
                "end_stress": 1.0,
                "end_energy": 1.0,
                "energy_consumption": 1.0,
                "main_stressor": None,
                "recovery_note": None,
                "free_text": None,
            },
            raw={},
        )
    assert "FOREIGN KEY" in str(captured.value).upper()
