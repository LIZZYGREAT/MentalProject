"""Opt-in PostgreSQL tests for row locks and uniqueness constraints.

Set MINDFLOW_TEST_POSTGRES_URL to a disposable database whose database name
contains ``test``. Each test run uses and drops its own random schema.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import threading
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.contracts.warning import WarningDeliveryPolicyConfig
from app.db import Base, Database, build_engine
from app.models import (
    CareInterventionEvent,
    CareInterventionFeedback,
    WarningSchedule,
)
from app.repositories import (
    CalendarSnapshotRepository,
    ForecastInputChangedError,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
    WarningScheduleRepository,
)
from app.repositories_care import (
    CareInterventionRepository,
    ParticipantCarePreferenceRepository,
)
from app.repositories_daily_review import DailyReviewScheduleRepository
from app.services.forecast_coordinator import _sha


@pytest.fixture
def postgres_database():
    raw_url = os.environ.get("MINDFLOW_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")
    parsed = make_url(raw_url)
    if not parsed.drivername.startswith("postgresql"):
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not PostgreSQL")
    if "test" not in str(parsed.database or "").casefold():
        pytest.fail("refusing PostgreSQL concurrency test outside a test database")

    schema = f"mindflow_test_{uuid.uuid4().hex}"
    root_engine = build_engine(raw_url)
    with root_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = root_engine.execution_options(
        schema_translate_map={None: schema}
    )
    Base.metadata.create_all(scoped_engine)
    try:
        yield Database(scoped_engine)
    finally:
        scoped_engine.dispose()
        with root_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root_engine.dispose()


def _runtime_repositories(database: Database):
    warnings = WarningScheduleRepository(
        database,
        WarningDeliveryPolicyConfig(max_daily_sends=2, min_interval_minutes=0),
        timezone_name="Asia/Shanghai",
    )
    preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=2,
        timezone_name="Asia/Shanghai",
    )
    return warnings, preferences


def _forecast(database: Database, participant_id: uuid.UUID, local_date):
    return ForecastSnapshotRepository(database).save(
        participant_id,
        local_date,
        calendar_revision="calendar",
        semantic_revision="semantic",
        observation_revision=_sha([]),
        algorithm_version="model",
        forecast_version=uuid.uuid4().hex,
        semantic_status="rules_only",
        semantic_input=[],
        curve=[],
        peaks=[],
        warning_windows=[],
        output={},
    )


def _warning(database: Database, participant_id: uuid.UUID):
    warnings, preferences = _runtime_repositories(database)
    now = datetime.now(timezone.utc)
    local_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    forecast = _forecast(database, participant_id, local_date)
    warnings.sync(
        participant_id,
        local_date,
        forecast_id=uuid.UUID(forecast["id"]),
        forecast_version=forecast["forecast_version"],
        warnings=[{
            "warning_identity": "postgres-race",
            "episode_identity": "postgres-race",
            "target_time": now,
            "valid_until": now + timedelta(minutes=20),
            "risk_time": now + timedelta(minutes=30),
            "warning_level": "2",
            "payload": {"message": "test"},
        }],
        now=now,
    )
    with database.session() as session:
        warning_id = session.query(WarningSchedule.id).scalar()
    return warnings, preferences, warning_id, now


def test_postgres_different_callback_snoozes_create_one_child(postgres_database):
    participant = ParticipantRepository(postgres_database).create("PG-SNOOZE")
    warnings, preferences, warning_id, now = _warning(
        postgres_database, participant.id
    )
    claimed = warnings.claim_if_current(warning_id, now=now)
    assert claimed is not None
    assert warnings.finish_claim(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=claimed["forecast_version"],
        sent=True,
        now=now + timedelta(seconds=1),
    )
    interventions = CareInterventionRepository(postgres_database, preferences)
    barrier = threading.Barrier(2)

    def snooze(callback_id: str):
        barrier.wait()
        return interventions.apply_action(
            participant.id,
            warning_id,
            action="snooze_30",
            callback_event_id=callback_id,
            now=now + timedelta(seconds=2),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(snooze, ("pg-snooze-a", "pg-snooze-b")))

    assert len({result["follow_up_warning_id"] for result in results}) == 1
    with postgres_database.session() as session:
        assert session.query(WarningSchedule).count() == 2
        assert session.query(CareInterventionFeedback).count() == 1


def test_postgres_preference_and_claim_race_cannot_leave_claimed_warning(
    postgres_database,
):
    participant = ParticipantRepository(postgres_database).create("PG-PREFERENCE")
    warnings, preferences, warning_id, now = _warning(
        postgres_database, participant.id
    )
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return warnings.claim_if_current(warning_id, now=now)

    def disable():
        barrier.wait()
        return preferences.update(
            participant.id, {"warning_enabled": False}, now=now
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        disable_future = executor.submit(disable)
        claim_future.result()
        disable_future.result()

    with postgres_database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "cancelled"
        assert row.claim_token is None


def test_postgres_mute_today_and_finish_claim_share_lock_order(postgres_database):
    participant = ParticipantRepository(postgres_database).create("PG-MUTE-FINISH")
    warnings, preferences, warning_id, now = _warning(
        postgres_database, participant.id
    )
    claimed = warnings.claim_if_current(warning_id, now=now)
    assert claimed is not None
    barrier = threading.Barrier(2)

    def finish():
        barrier.wait()
        return warnings.finish_claim(
            warning_id,
            claim_token=claimed["claim_token"],
            expected_forecast_version=claimed["forecast_version"],
            sent=True,
            now=now + timedelta(seconds=1),
        )

    def mute():
        barrier.wait()
        return preferences.mute_today(
            participant.id,
            now.astimezone(ZoneInfo("Asia/Shanghai")).date(),
            now=now + timedelta(seconds=1),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        finish_future = executor.submit(finish)
        mute_future = executor.submit(mute)
        finish_future.result(timeout=10)
        mute_future.result(timeout=10)

    with postgres_database.session() as session:
        warning = session.get(WarningSchedule, warning_id)
        intervention = session.execute(
            session.query(CareInterventionEvent).filter(
                CareInterventionEvent.source_warning_id == warning_id
            ).statement
        ).scalar_one()
        assert warning.status in {"sent", "cancelled"}
        assert intervention.delivery_status == warning.status


def test_postgres_disable_follow_up_and_snooze_are_serialized(postgres_database):
    participant = ParticipantRepository(postgres_database).create("PG-FOLLOWUP-RACE")
    warnings, preferences, warning_id, now = _warning(
        postgres_database, participant.id
    )
    claimed = warnings.claim_if_current(warning_id, now=now)
    assert claimed is not None
    assert warnings.finish_claim(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=claimed["forecast_version"],
        sent=True,
        now=now + timedelta(seconds=1),
    )
    interventions = CareInterventionRepository(postgres_database, preferences)
    barrier = threading.Barrier(2)

    def snooze():
        barrier.wait()
        return interventions.apply_action(
            participant.id,
            warning_id,
            action="snooze_30",
            callback_event_id="pg-followup-race",
            now=now + timedelta(seconds=2),
        )

    def disable():
        barrier.wait()
        return preferences.update(
            participant.id,
            {"allow_follow_up": False},
            now=now + timedelta(seconds=2),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        snooze_future = executor.submit(snooze)
        disable_future = executor.submit(disable)
        result = snooze_future.result(timeout=10)
        disable_future.result(timeout=10)

    with postgres_database.session() as session:
        children = session.query(WarningSchedule).filter(
            WarningSchedule.snoozed_from_intervention_id == warning_id
        ).all()
        assert len(children) <= 1
        if children:
            assert children[0].status == "cancelled"
        else:
            assert result["action_result"] == "follow_up_disabled"


def test_postgres_observation_save_race_never_leaves_stale_forecast_valid(
    postgres_database,
):
    participant = ParticipantRepository(postgres_database).create("PG-FORECAST")
    warnings, _preferences = _runtime_repositories(postgres_database)
    observations = ObservationRepository(postgres_database)
    forecasts = ForecastSnapshotRepository(postgres_database)
    calendars = CalendarSnapshotRepository(postgres_database)
    now = datetime.now(timezone.utc)
    timezone_value = ZoneInfo("Asia/Shanghai")
    local_date = now.astimezone(timezone_value).date()
    day_start = datetime.combine(
        local_date, datetime.min.time(), timezone_value
    ).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    calendars.upsert(
        participant.id,
        local_date,
        revision="calendar-current",
        events=[],
        degraded=False,
    )
    barrier = threading.Barrier(2)

    def publish():
        barrier.wait()
        try:
            forecasts.save(
                participant.id,
                local_date,
                calendar_revision="calendar-current",
                semantic_revision="semantic",
                observation_revision=_sha([]),
                algorithm_version="model",
                forecast_version="calculated-before-observation",
                semantic_status="rules_only",
                semantic_input=[],
                curve=[],
                peaks=[],
                warning_windows=[],
                output={},
                observation_window_start=day_start,
                observation_window_end=day_end,
                verify_current_inputs=True,
            )
        except ForecastInputChangedError:
            pass

    def observe_and_invalidate():
        barrier.wait()
        observations.add(
            participant.id,
            "checkin",
            {"stress_0_10": 8, "energy_0_10": 2},
            observed_at=now,
            source_message_id="postgres-observation",
        )
        forecasts.invalidate_current_for_date(
            warnings,
            participant.id,
            local_date,
            reason="postgres_race_test",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish), executor.submit(observe_and_invalidate)]
        for future in futures:
            future.result()

    assert forecasts.latest(participant.id, local_date) is None


def test_postgres_calendar_mutation_race_never_reactivates_old_revision(
    postgres_database,
):
    participant = ParticipantRepository(postgres_database).create("PG-CALENDAR")
    warnings, _preferences = _runtime_repositories(postgres_database)
    forecasts = ForecastSnapshotRepository(postgres_database)
    calendars = CalendarSnapshotRepository(postgres_database)
    now = datetime.now(timezone.utc)
    local_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    calendars.upsert(
        participant.id,
        local_date,
        revision="calendar-before-mutation",
        events=[],
        degraded=False,
    )
    barrier = threading.Barrier(2)

    def publish_old_revision():
        barrier.wait()
        try:
            forecasts.save(
                participant.id,
                local_date,
                calendar_revision="calendar-before-mutation",
                semantic_revision="semantic",
                observation_revision=_sha([]),
                algorithm_version="model",
                forecast_version="calculated-before-calendar-mutation",
                semantic_status="rules_only",
                semantic_input=[],
                curve=[],
                peaks=[],
                warning_windows=[],
                output={},
                verify_current_inputs=True,
            )
        except ForecastInputChangedError:
            pass

    def invalidate_calendar():
        barrier.wait()
        forecasts.invalidate_for_calendar_mutation(
            warnings,
            participant.id,
            local_date,
            reason="postgres_calendar_race_test",
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(publish_old_revision),
            executor.submit(invalidate_calendar),
        ]
        for future in futures:
            future.result()

    assert forecasts.latest(participant.id, local_date) is None


@pytest.mark.parametrize("provider_outcome", ["success", "failure"])
def test_postgres_provider_readback_cas_cannot_cross_calendar_mutation(
    postgres_database,
    provider_outcome,
):
    participant = ParticipantRepository(postgres_database).create(
        f"PG-CALENDAR-CAS-{provider_outcome}"
    )
    warnings, _preferences = _runtime_repositories(postgres_database)
    forecasts = ForecastSnapshotRepository(postgres_database)
    calendars = CalendarSnapshotRepository(postgres_database)
    local_date = datetime.now(timezone.utc).astimezone(
        ZoneInfo("Asia/Shanghai")
    ).date()
    calendars.upsert(
        participant.id,
        local_date,
        revision="calendar-before-provider-read",
        events=[{"id": "old-event"}],
        degraded=False,
    )
    expected = calendars.get(participant.id, local_date)
    barrier = threading.Barrier(2)
    provider_rejected = []

    def commit_provider_result():
        barrier.wait()
        try:
            kwargs = {
                "expected_snapshot_id": expected["id"],
                "expected_revision": expected["calendar_revision"],
                "expected_state": expected["snapshot_state"],
            }
            if provider_outcome == "success":
                calendars.commit_provider_read(
                    participant.id,
                    local_date,
                    **kwargs,
                    revision="stale-provider-response",
                    events=[{"id": "stale-event"}],
                )
            else:
                calendars.commit_provider_failure(
                    participant.id,
                    local_date,
                    **kwargs,
                    error_class="TimeoutError",
                    empty_revision="empty",
                )
        except ForecastInputChangedError:
            provider_rejected.append(True)

    def mutate_calendar():
        barrier.wait()
        forecasts.invalidate_for_calendar_mutation(
            warnings,
            participant.id,
            local_date,
            reason="postgres_provider_readback_cas",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(commit_provider_result),
            executor.submit(mutate_calendar),
        ]
        for future in futures:
            future.result(timeout=10)

    final = calendars.get(participant.id, local_date)
    assert final["snapshot_state"] == "mutation_refresh_pending"
    assert final["calendar_revision"].startswith("mutation:")
    assert provider_rejected in ([], [True])


def test_postgres_daily_review_disable_and_claim_race_ends_cancelled(
    postgres_database,
):
    participant = ParticipantRepository(postgres_database).create("PG-DAILY")
    _warnings, preferences = _runtime_repositories(postgres_database)
    schedules = DailyReviewScheduleRepository(
        postgres_database, timezone_name="Asia/Shanghai"
    )
    now = datetime.now(timezone.utc)
    review = schedules.ensure(
        participant.id,
        now.astimezone(ZoneInfo("Asia/Shanghai")).date(),
        now - timedelta(minutes=1),
        valid_until=now + timedelta(hours=1),
    )
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return schedules.claim_due(now, lease_seconds=120)

    def disable():
        barrier.wait()
        return preferences.update(
            participant.id, {"daily_review_enabled": False}, now=now
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        disable_future = executor.submit(disable)
        claim_future.result()
        disable_future.result()

    stored = schedules.get(review["id"])
    assert stored["status"] == "cancelled"
    assert stored["claim_token"] is None
