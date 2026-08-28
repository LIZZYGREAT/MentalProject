"""Opt-in PostgreSQL tests for row locks and uniqueness constraints.

Set MINDFLOW_TEST_POSTGRES_URL to a disposable database whose database name
contains ``test``. Each test run uses and drops its own random schema.
"""

from concurrent.futures import ThreadPoolExecutor
import asyncio
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
    ForecastCurrentnessEvent,
    WarningSchedule,
)
from app.repositories import (
    CalendarSnapshotRepository,
    EventSemanticCacheRepository,
    ForecastInputChangedError,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
    WarningScheduleRepository,
)
from app.repositories_calendar_mutation import (
    CalendarMutationReconciliationRepository,
)
from app.repositories_care import (
    CareInterventionRepository,
    ParticipantCarePreferenceRepository,
)
from helpers import seed_calendar_snapshot
from app.repositories_daily_review import DailyReviewScheduleRepository
from app.services.forecast_coordinator import _sha
from app.services.token_service import (
    OAuthTokenSet,
    TokenEncryptionService,
    TokenRefreshService,
    TokenRepository,
)


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


def test_postgres_calendar_recovery_claim_has_one_winner(postgres_database):
    participant = ParticipantRepository(postgres_database).create(
        "PG-CALENDAR-RECOVERY-CLAIM"
    )
    reconciliations = CalendarMutationReconciliationRepository(postgres_database)
    target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    intent = reconciliations.create(
        participant.id,
        mutation_kind="calendar_create_event",
        direct_dates={target},
        refresh_targets={target: True},
        dependency_sources={},
    )
    reconciliations.mark_remote_committed(intent["id"])
    barrier = threading.Barrier(2)
    tokens = (uuid.uuid4(), uuid.uuid4())

    def claim(token):
        barrier.wait(timeout=2)
        return reconciliations.claim_processing(
            intent["id"], claim_token=token, lease_seconds=60
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, tokens))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    winning_token = uuid.UUID(winners[0]["work"]["processing_claim_token"])
    assert reconciliations.release_processing(
        intent["id"], claim_token=winning_token
    )
    assert len(reconciliations.due()) == 1


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("complete", "rejected", "complete"),
        ("complete", "partial", "complete"),
        ("partial", "rejected", "partial"),
        ("rejected", "complete", "complete"),
    ],
)
def test_postgres_semantic_cache_concurrent_quality_is_monotonic(
    postgres_database, left, right, expected
):
    participant = ParticipantRepository(postgres_database).create(
        f"PG-SEMANTIC-{left}-{right}"
    )
    fingerprint = uuid.uuid4().hex * 2
    barrier = threading.Barrier(2)

    def write(status):
        cache = EventSemanticCacheRepository(postgres_database)
        barrier.wait(timeout=2)
        common = {
            "schema_version": "event_semantics.v3",
            "prompt_version": "postgres-precedence.v1",
            "model": "postgres-semantic-test",
        }
        if status == "complete":
            cache.put_complete(
                participant.id, fingerprint, {"writer": status}, **common
            )
        elif status == "partial":
            cache.put_partial(
                participant.id, fingerprint, {"writer": status}, **common
            )
        else:
            cache.put_rejected(
                participant.id,
                fingerprint,
                reason="postgres_race_rejection",
                confidence=0.1,
                assessment={"writer": status},
                **common,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(write, status) for status in (left, right)]
        for result in results:
            result.result(timeout=5)

    entry = EventSemanticCacheRepository(postgres_database).get_entry(
        participant.id,
        fingerprint,
        schema_version="event_semantics.v3",
        prompt_version="postgres-precedence.v1",
        model="postgres-semantic-test",
    )
    assert entry["status"] == expected
    assert entry["assessment"]["writer"] == expected


def test_postgres_different_callback_snoozes_create_one_child(postgres_database):
    participant = ParticipantRepository(postgres_database).create("PG-SNOOZE")
    warnings, preferences, warning_id, now = _warning(
        postgres_database, participant.id
    )
    claimed = warnings.claim_if_current(warning_id, now=now)
    assert claimed is not None
    assert warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=claimed["forecast_version"],
        now=now + timedelta(milliseconds=500),
    )
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
    assert warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=claimed["forecast_version"],
        now=now + timedelta(milliseconds=500),
    )
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
    seed_calendar_snapshot(
        postgres_database,
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
    seed_calendar_snapshot(
        postgres_database,
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
    seed_calendar_snapshot(
        postgres_database,
        participant.id,
        local_date,
        revision="calendar-before-provider-read",
        events=[{"id": "old-event"}],
        degraded=False,
    )
    expected = calendars.get(participant.id, local_date)
    barrier = threading.Barrier(2)

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
            pass

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


def test_postgres_forecast_currentness_activate_reactivate_history(
    postgres_database,
):
    participant = ParticipantRepository(postgres_database).create("PG-CURRENTNESS")
    repository = ForecastSnapshotRepository(postgres_database)
    local_date = datetime.now(timezone.utc).astimezone(
        ZoneInfo("Asia/Shanghai")
    ).date()

    def save(version):
        return repository.save(
            participant.id,
            local_date,
            calendar_revision="calendar",
            semantic_revision="semantic",
            algorithm_version="mindflow-ctssm-runtime-v7",
            forecast_version=version,
            semantic_status="complete",
            semantic_input=[],
            curve=[],
            peaks=[],
            warning_windows=[],
            output={"stress_0_10": 4, "vitality_0_10": 5},
        )

    v1 = save("pg-currentness-v1")
    v2 = save("pg-currentness-v2")
    save("pg-currentness-v1")
    with postgres_database.session() as session:
        activation_times = [
            event.occurred_at
            for event in session.query(ForecastCurrentnessEvent).filter(
                ForecastCurrentnessEvent.participant_id == participant.id,
                ForecastCurrentnessEvent.local_date == local_date,
                ForecastCurrentnessEvent.event_type == "activated",
            ).order_by(ForecastCurrentnessEvent.id).all()
        ]
    t1, t2, t3 = activation_times

    assert repository.current_at(participant.id, local_date, t1)["id"] == v1["id"]
    assert repository.current_at(participant.id, local_date, t2)["id"] == v2["id"]
    assert repository.current_at(participant.id, local_date, t3)["id"] == v1["id"]


def test_postgres_oauth_refresh_lease_has_one_authoritative_owner(
    postgres_database,
):
    participant = ParticipantRepository(postgres_database).create("PG-OAUTH-LEASE")
    encryption = TokenEncryptionService(TokenEncryptionService.generate_key())
    repository = TokenRepository(
        postgres_database, encryption, oauth_app_id="calendar-app"
    )
    now = datetime.now(timezone.utc)
    repository.save(participant.id, OAuthTokenSet(
        access_token="expired",
        refresh_token="refresh-old",
        access_token_expires_at=now - timedelta(seconds=1),
        refresh_token_expires_at=now + timedelta(days=7),
    ))
    refresh_count = 0
    network_started = asyncio.Event()

    async def refresh(value):
        nonlocal refresh_count
        assert value == "refresh-old"
        refresh_count += 1
        network_started.set()
        await asyncio.sleep(0.1)
        return OAuthTokenSet(
            access_token="access-new",
            refresh_token="refresh-new",
            access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            refresh_token_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )

    services = [
        TokenRefreshService(
            postgres_database,
            encryption,
            refresh,
            expected_oauth_app_id="calendar-app",
            refresh_poll_seconds=0.01,
        )
        for _ in range(2)
    ]

    async def scenario():
        requests = [
            asyncio.create_task(service.get_access_token(participant.id))
            for service in services
        ]
        await asyncio.wait_for(network_started.wait(), timeout=2)
        # If a row lock were held across HTTP, this status read would block.
        status = await asyncio.wait_for(
            asyncio.to_thread(repository.status, participant.id), timeout=0.5
        )
        assert status["connected"] is True
        return await asyncio.gather(*requests)

    assert asyncio.run(scenario()) == ["access-new", "access-new"]
    assert refresh_count == 1
    assert repository.status(participant.id)["token_version"] == 2
