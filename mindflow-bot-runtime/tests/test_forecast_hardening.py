import asyncio
from datetime import date, datetime, timedelta, timezone
import io
import inspect
import logging
import uuid
from zoneinfo import ZoneInfo

import pytest

from algorithm.dynamic_state_model import assess_event
from app.agent.context import AgentContext
from app.contracts.warning import WarningDeliveryPolicyConfig
from app.integrations.feishu.client import FeishuSendError
from app.integrations.feishu.gateway import FeishuGateway
from app.logging_security import install_credential_redaction
from app.models import FeishuOAuthToken, ForecastSnapshot, WarningSchedule
from app.repositories import (
    BindingRepository,
    CalendarSnapshotRepository,
    ForecastInputChangedError,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
)
from app.services.forecast_coordinator import ForecastCoordinator, _sha
from app.services.forecast_scheduler import ForecastScheduler
from app.services.warning_policy import WarningPolicy
from app.tools.care import CareTools
from helpers import memory_database, seed_calendar_snapshot
from services.semantic_model_inputs import semantic_model_inputs
from services.event_semantics import DIMENSIONS, validate_external_semantics
from tests.test_forecast_pipeline import build_pipeline, event
from utils.event_factory import EventFactory


TEST_LOCAL_DATE = date(2030, 1, 15)
TEST_NOW = datetime(2030, 1, 15, 5, 45, tzinfo=timezone.utc)


def test_single_flight_forced_same_mode_update_runs_followup_before_completion():
    async def scenario():
        coordinator = object.__new__(ForecastCoordinator)
        coordinator._inflight = {}
        coordinator._guard = asyncio.Lock()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def ensure_once(_participant_id, _target, reason, **kwargs):
            calls.append((reason, kwargs))
            if len(calls) == 1:
                started.set()
                await release.wait()
            return {"call": len(calls), "reason": reason}

        coordinator._ensure_once = ensure_once
        participant_id = uuid.uuid4()
        first = asyncio.create_task(coordinator.ensure_forecast(
            participant_id,
            TEST_LOCAL_DATE,
            "initial",
            refresh_calendar=False,
        ))
        await started.wait()
        forced = asyncio.create_task(coordinator.ensure_forecast(
            participant_id,
            TEST_LOCAL_DATE,
            "observation_committed",
            refresh_calendar=False,
            force_followup=True,
        ))
        await asyncio.sleep(0)
        release.set()
        return await first, await forced, calls

    first, forced, calls = asyncio.run(scenario())

    assert len(calls) == 2
    assert calls[1][0] == "observation_committed"
    assert calls[1][1]["refresh_calendar"] is False
    assert first == forced == {"call": 2, "reason": "observation_committed"}


def test_single_flight_failure_does_not_drop_already_dirty_followup():
    async def scenario():
        coordinator = object.__new__(ForecastCoordinator)
        coordinator._inflight = {}
        coordinator._guard = asyncio.Lock()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def ensure_once(_participant_id, _target, reason, **kwargs):
            calls.append((reason, kwargs))
            if len(calls) == 1:
                started.set()
                await release.wait()
                raise RuntimeError("transient first-generation failure")
            return {"call": len(calls), "reason": reason}

        coordinator._ensure_once = ensure_once
        participant_id = uuid.uuid4()
        first = asyncio.create_task(coordinator.ensure_forecast(
            participant_id,
            TEST_LOCAL_DATE,
            "initial",
            refresh_calendar=False,
        ))
        await started.wait()
        forced = asyncio.create_task(coordinator.ensure_forecast(
            participant_id,
            TEST_LOCAL_DATE,
            "observation_committed",
            refresh_calendar=False,
            force_followup=True,
        ))
        await asyncio.sleep(0)
        release.set()
        return await first, await forced, calls

    first, forced, calls = asyncio.run(scenario())

    assert len(calls) == 2
    assert calls[1][0] == "observation_committed"
    assert first == forced == {"call": 2, "reason": "observation_committed"}


def test_forecast_save_rejects_observation_revision_that_lost_a_race():
    database = memory_database()
    participant = ParticipantRepository(database).create("FORECAST-REVISION-FENCE")
    observations = ObservationRepository(database)
    snapshots = CalendarSnapshotRepository(database)
    forecasts = ForecastSnapshotRepository(database)
    local_timezone = ZoneInfo("Asia/Shanghai")
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    target = observed_at.astimezone(local_timezone).date()
    day_start = datetime.combine(target, datetime.min.time(), local_timezone).astimezone(
        timezone.utc
    )
    day_end = day_start + timedelta(days=1)
    seed_calendar_snapshot(
        database,
        participant.id,
        target,
        revision="calendar-current",
        events=[],
        degraded=False,
    )
    observations.add(
        participant.id,
        "checkin",
        {"stress_0_10": 4, "energy_0_10": 6},
        observed_at=observed_at,
        source_message_id="before-calculation",
    )
    calculation_cutoff = datetime.now(timezone.utc)
    calculated_rows = observations.for_local_date(
        participant.id,
        target,
        timezone_name="Asia/Shanghai",
        as_of=calculation_cutoff,
    )
    calculated_revision = _sha(calculated_rows)
    observations.add(
        participant.id,
        "checkin",
        {"stress_0_10": 8, "energy_0_10": 2},
        observed_at=calculation_cutoff - timedelta(seconds=1),
        source_message_id="won-race",
    )

    with pytest.raises(ForecastInputChangedError) as error:
        forecasts.save(
            participant.id,
            target,
            calendar_revision="calendar-current",
            semantic_revision="semantic",
            observation_revision=calculated_revision,
            algorithm_version="model",
            forecast_version="stale-version",
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

    assert error.value.input_name == "observation"
    assert forecasts.latest(participant.id, target) is None


def save_forecast_and_warnings(
    database, participant, warnings, *, version, items, now=TEST_NOW,
):
    forecasts = ForecastSnapshotRepository(database)
    serialized = [{
        **item,
        "target_time": item["target_time"].isoformat(),
        "risk_time": item["risk_time"].isoformat(),
        "valid_until": item["valid_until"].isoformat(),
    } for item in items]
    return forecasts.save_and_sync_warnings(
        warnings, participant.id, TEST_LOCAL_DATE,
        calendar_revision=f"calendar-{version}",
        semantic_revision=f"semantic-{version}",
        algorithm_version="algorithm", forecast_version=version,
        semantic_status="rules_only", semantic_input=[], curve=[], peaks=[],
        warning_windows=serialized, output={}, warnings=items, now=now,
    )


def supersede_claimed_warning(
    database, participant, warnings, warning_id, *, now=TEST_NOW,
):
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        episode = row.episode_identity
        target_time = row.target_time
        risk_time = row.risk_time
        valid_until = row.valid_until
    saved, _ = save_forecast_and_warnings(
        database, participant, warnings, version="new-forecast-version", now=now,
        items=[{
            "warning_identity": episode, "episode_identity": episode,
            "target_time": target_time, "risk_time": risk_time,
            "valid_until": valid_until, "warning_level": "3",
            "episode_drift_minutes": 15,
            "payload": {"message": "NEW RED WARNING"},
        }],
    )
    return saved


def semantic(appraisal, difficulty=0.6):
    values = {
        "difficulty": difficulty, "cognitive_demand": 0.6, "stakes": 0.5,
        "time_pressure": 0.4, "social_evaluation": 0.4,
        "uncontrollability": 0.3, "novelty": 0.2,
        "expected_effort": 0.6, "uncertainty": 0.3, "unfinished": 0.2,
    }
    return {
        "values": values,
        "fused": {"objective_semantics": values, "appraisal_score_1_10": appraisal},
    }


def assessed(appraisal, explicit=None):
    metadata = {"semantic": semantic(appraisal)}
    if explicit is not None:
        metadata["appraisal"] = explicit
    model_event = EventFactory.create_from_json([{
        **event(), "event_type": "task", "task_type": "meeting",
        "metadata": metadata,
    }])[0]
    return assess_event(model_event)


def test_fused_appraisal_enters_model_and_explicit_appraisal_wins():
    negative = assessed(1)
    positive = assessed(10)
    assert negative.appraisal["threat"] > positive.appraisal["threat"]
    assert negative.appraisal["challenge"] < positive.appraisal["challenge"]
    explicit = assessed(1, {"threat": 0.05, "challenge": 0.95})
    assert explicit.appraisal["threat"] == 0.05
    assert explicit.appraisal["challenge"] == 0.95


def test_appraisal_is_part_of_model_projection_and_materiality():
    before = [{"id": "e", "metadata": {"semantic": semantic(5.0)}}]
    material = [{"id": "e", "metadata": {"semantic": semantic(9.0)}}]
    tiny = [{"id": "e", "metadata": {"semantic": semantic(5.01)}}]
    assert ForecastCoordinator._semantic_delta(before, material) >= 0.03
    assert ForecastCoordinator._semantic_delta(before, tiny) < 0.03
    assert semantic_model_inputs(semantic(9.0))["appraisal_f_like"] == 0.8


def _external_semantics(appraisal):
    return {
        "values": {key: 0.5 for key in DIMENSIONS},
        "appraisal_score_1_10": appraisal,
        "confidence": 0.8,
        "evidence_tags": [],
        "reasoning_summary": "ok",
    }


@pytest.mark.parametrize("appraisal", [1, 10, 5.5])
def test_external_appraisal_accepts_finite_values_in_range(appraisal):
    validate_external_semantics(_external_semantics(appraisal))


@pytest.mark.parametrize(
    "appraisal", [0, 11, float("nan"), float("inf"), float("-inf"), None, "abc"]
)
def test_external_appraisal_rejects_nonfinite_or_out_of_range_values(appraisal):
    with pytest.raises(ValueError, match="appraisal_score_1_10"):
        validate_external_semantics(_external_semantics(appraisal))


@pytest.mark.parametrize("callers", [2, 10])
def test_forecast_same_key_is_true_single_flight(callers):
    _, participant, _, _, _, _, coordinator = build_pipeline([event()])
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def ensure_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"call": calls}

    coordinator._ensure_once = ensure_once

    async def scenario():
        tasks = [
            asyncio.create_task(
                coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "query")
            )
            for _ in range(callers)
        ]
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks)
        assert results == [{"call": 1}] * callers

    asyncio.run(scenario())
    assert calls == 1


def test_stronger_calendar_refresh_coalesces_to_one_followup():
    _, participant, _, _, _, _, coordinator = build_pipeline([event()])
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def ensure_once(*_args, refresh_calendar, **_kwargs):
        calls.append(refresh_calendar)
        if len(calls) == 1:
            started.set()
            await release.wait()
        return {"refresh_calendar": refresh_calendar}

    coordinator._ensure_once = ensure_once

    async def scenario():
        baseline = asyncio.create_task(coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "semantic_completion",
            refresh_calendar=False,
        ))
        await started.wait()
        refreshes = [
            asyncio.create_task(coordinator.ensure_forecast(
                participant.id, TEST_LOCAL_DATE, "user_curve_request",
                refresh_calendar=True,
            ))
            for _ in range(9)
        ]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(baseline, *refreshes)
        assert all(result["refresh_calendar"] is True for result in results)

    asyncio.run(scenario())
    assert calls == [False, True]


def test_invalid_appraisal_is_not_cached_and_rules_remain_available(caplog):
    class Client:
        provider = "deepseek"
        model = "fake"

        def infer(self, _payload):
            return _external_semantics(float("nan"))

    _, participant, _, preprocessor, _, _, _ = build_pipeline(
        [event()], consent=True, client=Client()
    )
    cache_put_calls = 0
    original_put = preprocessor.cache.put_complete

    def count_put(*args, **kwargs):
        nonlocal cache_put_calls
        cache_put_calls += 1
        return original_put(*args, **kwargs)

    preprocessor.cache.put_complete = count_put

    async def scenario():
        prepared, _, status, misses = preprocessor.prepare(
            participant.id, [event()], consent=True
        )
        with caplog.at_level(logging.WARNING):
            await preprocessor.enqueue(participant.id, misses, lambda: asyncio.sleep(0))
            await preprocessor.close()
        assert status == "rules_only"
        assert prepared[0]["metadata"]["semantic"]["source"] == "rules"

    asyncio.run(scenario())
    assert cache_put_calls == 0
    assert "semantic_enrichment_failed" in caplog.text
    assert "error_class=SemanticResponseMalformedError" in caplog.text


def test_semantic_tasks_are_cleaned_and_close_is_bounded():
    class Client:
        provider = "deepseek"
        model = "fake"

        def infer(self, _payload):
            return {
                "values": {key: 0.5 for key in (
                    "difficulty", "cognitive_demand", "stakes", "time_pressure",
                    "social_evaluation", "uncontrollability", "novelty",
                    "expected_effort", "uncertainty", "unfinished",
                )},
                "appraisal_score_1_10": 5, "confidence": 0.8,
                "evidence_tags": [], "reasoning_summary": "ok",
            }

    _, participant, _, preprocessor, _, _, _ = build_pipeline(
        [event()], consent=True, client=Client()
    )

    async def scenario():
        misses = []
        for index in range(100):
            _, _, _, item_misses = preprocessor.prepare(
                participant.id,
                [event(summary=f"课程{index}")], consent=True,
            )
            misses.extend(item_misses)
        await preprocessor.enqueue(participant.id, misses, lambda: asyncio.sleep(0))
        await asyncio.sleep(0.5)
        await preprocessor.close(timeout_seconds=2)
        assert preprocessor._inflight == {}
        assert not preprocessor._completion_tasks

    asyncio.run(scenario())


def test_active_calendar_ids_excludes_participant_without_oauth():
    database = memory_database()
    participants = ParticipantRepository(database)
    connected = participants.create("CONNECTED")
    participants.create("NO-OAUTH")
    now = datetime.now(timezone.utc)
    with database.session() as session:
        session.add(FeishuOAuthToken(
            participant_id=connected.id, oauth_app_id="calendar-app",
            access_token_ciphertext="x",
            refresh_token_ciphertext="y", access_token_expires_at=now + timedelta(hours=1),
        ))
    assert participants.active_calendar_ids("calendar-app") == [connected.id]


def _calendar_ids_for_provider_rows(rows):
    database = memory_database()
    participants = ParticipantRepository(database)
    now = datetime.now(timezone.utc)
    created = [participants.create(code) for code, _ in rows]
    with database.session() as session:
        for person, (_, oauth_app_id) in zip(created, rows):
            session.add(FeishuOAuthToken(
                participant_id=person.id,
                oauth_app_id=oauth_app_id,
                access_token_ciphertext="x",
                refresh_token_ciphertext="y",
                access_token_expires_at=now + timedelta(hours=1),
            ))
    return created, participants.active_calendar_ids("calendar-app")


def test_active_calendar_ids_excludes_legacy_null_oauth_app():
    people, active_ids = _calendar_ids_for_provider_rows(
        [("CURRENT", "calendar-app"), ("LEGACY", None)]
    )
    assert active_ids == [people[0].id]


def test_active_calendar_ids_excludes_different_oauth_app():
    people, active_ids = _calendar_ids_for_provider_rows(
        [("CURRENT", "calendar-app"), ("DIFFERENT", "old-calendar-app")]
    )
    assert active_ids == [people[0].id]


def test_scheduler_queries_participants_for_current_calendar_app_and_bounds_concurrency(
    caplog,
):
    class Participants:
        def __init__(self):
            self.oauth_app_ids = []

        def active_calendar_ids(self, oauth_app_id):
            self.oauth_app_ids.append(oauth_app_id)
            return [uuid.uuid4() for _ in range(4)]

    class Coordinator:
        def __init__(self):
            self.running = 0
            self.maximum = 0
            self.calls = 0

        async def ensure_forecast(self, _pid, _target, _reason):
            self.running += 1
            self.maximum = max(self.maximum, self.running)
            self.calls += 1
            try:
                await asyncio.sleep(0.01)
                if self.calls == 2:
                    raise RuntimeError("safe failure")
            finally:
                self.running -= 1

    coordinator = Coordinator()
    participants = Participants()
    scheduler = ForecastScheduler(
        coordinator=coordinator, participants=participants, warnings=None,
        bindings=None, sender=None, timezone_name="Asia/Shanghai",
        calendar_oauth_app_id="calendar-app",
        daily_prepare_local_time="07:30", calendar_sync_interval_seconds=999,
        warning_poll_interval_seconds=999, forecast_max_concurrency=2,
        warning_delivery_policy=WarningDeliveryPolicyConfig(2, 240),
    )

    async def scenario():
        task = asyncio.create_task(scheduler._forecast_loop())
        while coordinator.calls < 8:
            await asyncio.sleep(0.01)
        await scheduler.close()
        await task

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert coordinator.maximum <= 2
    assert participants.oauth_app_ids
    assert set(participants.oauth_app_ids) == {"calendar-app"}
    assert "forecast_job_failed" in caplog.text


def test_warning_retry_lease_expiry_and_missing_channel():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        warning_id = row.id
        now = row.target_time + timedelta(seconds=1)
    claimed = warnings.claim_if_current(warning_id, now=now, lease_seconds=10)
    assert claimed is not None
    assert warnings.claim_if_current(warning_id, now=now + timedelta(seconds=5), lease_seconds=10) is None
    reclaimed = warnings.claim_if_current(
        warning_id, now=now + timedelta(seconds=11), lease_seconds=10
    )
    assert reclaimed is not None
    warnings.finish_claim(
        warning_id, claim_token=reclaimed["claim_token"],
        expected_forecast_version=reclaimed["forecast_version"], sent=False,
        now=now + timedelta(seconds=11), retryable=True,
        max_attempts=5, retry_base_seconds=60, error_class="FeishuSendError",
    )
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "pending"
        assert row.attempt_count == 1
        assert row.next_attempt_at >= now + timedelta(seconds=70)
        row.valid_until = now + timedelta(seconds=20)
    assert warnings.pending(now + timedelta(seconds=21)) == []
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "expired"


def test_unattempted_pending_warning_uses_earlier_rescheduled_window():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])
    asyncio.run(coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare"))
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        warning_id = row.id
        episode = row.episode_identity
        row.target_time = TEST_NOW + timedelta(minutes=30)
        row.valid_until = TEST_NOW + timedelta(minutes=40)
        row.risk_time = TEST_NOW + timedelta(minutes=50)
        row.next_attempt_at = row.target_time

    new_target = TEST_NOW + timedelta(minutes=10)
    saved, diff = save_forecast_and_warnings(
        database, participant, warnings, version="earlier-window", items=[{
            "warning_identity": episode, "episode_identity": episode,
            "target_time": new_target,
            "valid_until": TEST_NOW + timedelta(minutes=20),
            "risk_time": TEST_NOW + timedelta(minutes=30),
            "warning_level": "2", "episode_drift_minutes": 15,
            "payload": {"message": "moved earlier"},
        }],
    )

    assert diff["rescheduled"] == 1
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.id == warning_id
        assert row.forecast_id == uuid.UUID(saved["id"])
        assert row.forecast_version == "earlier-window"
        assert warnings._aware(row.next_attempt_at) == new_target
        forecast = session.get(ForecastSnapshot, row.forecast_id)
        assert forecast.valid is True
        assert forecast.forecast_version == row.forecast_version
    assert [item["id"] for item in warnings.pending(new_target)] == [str(warning_id)]


def test_unattempted_warning_rescheduled_into_open_window_is_due_immediately():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])
    asyncio.run(coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare"))
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        warning_id = row.id
        episode = row.episode_identity
        row.target_time = TEST_NOW + timedelta(minutes=20)
        row.valid_until = TEST_NOW + timedelta(minutes=30)
        row.risk_time = TEST_NOW + timedelta(minutes=40)
        row.next_attempt_at = row.target_time

    save_forecast_and_warnings(
        database, participant, warnings, version="open-window", items=[{
            "warning_identity": episode, "episode_identity": episode,
            "target_time": TEST_NOW - timedelta(minutes=5),
            "valid_until": TEST_NOW + timedelta(minutes=5),
            "risk_time": TEST_NOW + timedelta(minutes=15),
            "warning_level": "2", "episode_drift_minutes": 15,
            "payload": {"message": "already open"},
        }],
    )

    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert warnings._aware(row.next_attempt_at) == TEST_NOW
        assert row.attempt_count == 0
    assert [item["id"] for item in warnings.pending(TEST_NOW)] == [str(warning_id)]


def test_forecast_metadata_update_preserves_existing_retry_backoff():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])
    asyncio.run(coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare"))
    target = TEST_NOW - timedelta(minutes=1)
    valid_until = TEST_NOW + timedelta(minutes=20)
    risk_time = TEST_NOW + timedelta(minutes=30)
    backoff_deadline = TEST_NOW + timedelta(minutes=8)
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        warning_id = row.id
        episode = row.episode_identity
        row.target_time = target
        row.valid_until = valid_until
        row.risk_time = risk_time
        row.attempt_count = 1
        row.next_attempt_at = backoff_deadline

    save_forecast_and_warnings(
        database, participant, warnings, version="metadata-only", items=[{
            "warning_identity": episode, "episode_identity": episode,
            "target_time": target, "valid_until": valid_until, "risk_time": risk_time,
            "warning_level": "2", "episode_drift_minutes": 15,
            "payload": {"message": "updated payload"},
        }],
    )

    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert warnings._aware(row.next_attempt_at) == backoff_deadline
        assert row.attempt_count == 1
    assert warnings.pending(backoff_deadline - timedelta(seconds=1)) == []
    assert [item["id"] for item in warnings.pending(backoff_deadline)] == [str(warning_id)]


def test_forecast_activation_and_warning_rebind_roll_back_together(monkeypatch):
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])
    asyncio.run(coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare"))
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        warning_id = row.id
        old_forecast_id = row.forecast_id
        old_forecast_version = row.forecast_version
        item = {
            "warning_identity": row.warning_identity,
            "episode_identity": row.episode_identity,
            "target_time": row.target_time,
            "valid_until": row.valid_until,
            "risk_time": row.risk_time,
            "warning_level": row.warning_level,
            "episode_drift_minutes": 15,
            "payload": dict(row.payload_json),
        }

    original_sync = warnings._sync_in_session

    def fail_after_sync(*args, **kwargs):
        original_sync(*args, **kwargs)
        raise RuntimeError("simulated warning sync failure")

    monkeypatch.setattr(warnings, "_sync_in_session", fail_after_sync)
    with pytest.raises(RuntimeError, match="simulated warning sync failure"):
        save_forecast_and_warnings(
            database, participant, warnings, version="must-roll-back", items=[item],
        )

    with database.session() as session:
        forecasts = session.query(ForecastSnapshot).all()
        assert len(forecasts) == 1
        assert forecasts[0].id == old_forecast_id
        assert forecasts[0].valid is True
        row = session.get(WarningSchedule, warning_id)
        assert row.forecast_id == old_forecast_id
        assert row.forecast_version == old_forecast_version


def test_warning_window_late_grace_stays_before_risk_time():
    _, _, _, _, _, _, coordinator = build_pipeline([event()])
    warning = coordinator._warning_windows(
        [{"time": "14:00", "tier": 2}], TEST_LOCAL_DATE
    )[0]
    local_tz = timezone(timedelta(hours=8))
    assert warning["target_time"].astimezone(local_tz).strftime("%H:%M") == "13:40"
    assert warning["valid_until"].astimezone(local_tz).strftime("%H:%M") == "13:50"
    assert warning["valid_until"] < warning["risk_time"]


def test_warning_can_send_slightly_late_but_expires_at_risk_time():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.target_time = TEST_NOW - timedelta(minutes=5)
        row.valid_until = TEST_NOW + timedelta(minutes=5)
        row.risk_time = TEST_NOW + timedelta(minutes=15)
        row.next_attempt_at = row.target_time
        warning_id = row.id
    assert [item["id"] for item in warnings.pending(TEST_NOW)] == [str(warning_id)]
    claimed = warnings.claim_if_current(warning_id, now=TEST_NOW)
    assert claimed is not None
    warnings.finish_claim(
        warning_id, claim_token=claimed["claim_token"],
        expected_forecast_version=claimed["forecast_version"], sent=False,
        now=TEST_NOW, retryable=True,
        retry_base_seconds=60,
    )
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        row.status = "pending"
        row.next_attempt_at = TEST_NOW
    late_due = warnings.pending(TEST_NOW + timedelta(minutes=15))
    assert len(late_due) == 1
    assert late_due[0]["id"] != str(warning_id)
    assert late_due[0]["payload"]["delivery_kind"] == "same_day_late_care"
    assert "即将" not in late_due[0]["payload"]["message"]
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "expired"


def test_warning_retry_crossing_risk_time_expires_instead_of_rescheduling():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.status = "claimed"
        row.claim_token = uuid.uuid4()
        row.risk_time = TEST_NOW + timedelta(seconds=30)
        row.valid_until = TEST_NOW + timedelta(seconds=30)
        warning_id = row.id
        claim_token = row.claim_token
        forecast_version = row.forecast_version
    warnings.finish_claim(
        warning_id, claim_token=claim_token,
        expected_forecast_version=forecast_version, sent=False,
        now=TEST_NOW, retryable=True,
        retry_base_seconds=60,
    )
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "expired"
        assert row.next_attempt_at is None
    late_due = warnings.pending(TEST_NOW + timedelta(seconds=30))
    assert len(late_due) == 1
    assert late_due[0]["payload"]["source_opportunity_id"] == str(warning_id)


def test_delivery_authorization_rechecks_risk_time_after_claim_race():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.status = "pending"
        row.risk_time = TEST_NOW
        row.valid_until = TEST_NOW + timedelta(minutes=5)
        warning_id = row.id
        forecast_version = row.forecast_version
    claim = warnings.claim_if_current(
        warning_id,
        now=TEST_NOW - timedelta(seconds=1),
    )
    assert claim is not None
    assert warnings.validate_claim_current(
        warning_id,
        claim_token=claim["claim_token"],
        expected_forecast_version=forecast_version,
        now=TEST_NOW,
    ) is False
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "expired"


class _WarningDeliveryDateTime(datetime):
    current = TEST_NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


def _warning_delivery_fixture(monkeypatch, sender, *, lease_seconds=1, retry_seconds=1):
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])
    asyncio.run(
        coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")
    )
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.target_time = TEST_NOW - timedelta(seconds=1)
        row.next_attempt_at = TEST_NOW - timedelta(seconds=1)
        row.valid_until = TEST_NOW + timedelta(minutes=10)
        row.risk_time = TEST_NOW + timedelta(minutes=20)
        row.payload_json = {**dict(row.payload_json), "message": "stable warning"}
        warning_id = row.id

    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc_test"}

    _WarningDeliveryDateTime.current = TEST_NOW
    monkeypatch.setattr(
        "app.services.forecast_scheduler.datetime", _WarningDeliveryDateTime,
    )
    scheduler = ForecastScheduler(
        coordinator=coordinator, participants=None, warnings=warnings,
        bindings=Bindings(), sender=sender, timezone_name="Asia/Shanghai",
        calendar_oauth_app_id="calendar-app",
        daily_prepare_local_time="07:30", calendar_sync_interval_seconds=999,
        warning_poll_interval_seconds=999,
        warning_claim_lease_seconds=lease_seconds,
        warning_retry_base_seconds=retry_seconds,
    )
    return database, warnings, scheduler, warning_id


def test_warning_send_uses_warning_id_as_feishu_message_uuid(monkeypatch):
    class Sender:
        def __init__(self):
            self.calls = []

        def send_text(self, chat_id, text, *, message_uuid=None):
            self.calls.append((chat_id, text, message_uuid))
            return "om-warning"

    sender = Sender()
    database, warnings, scheduler, warning_id = _warning_delivery_fixture(
        monkeypatch, sender,
    )
    asyncio.run(scheduler._deliver_warning(warnings.pending(TEST_NOW)[0]))

    assert sender.calls == [("oc_test", "stable warning", str(warning_id))]
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "sent"


def test_reclaimed_warning_reuses_same_feishu_message_uuid(monkeypatch):
    class CrashAfterProviderSuccess(BaseException):
        pass

    class Sender:
        def __init__(self):
            self.calls = []
            self.crash = True

        def send_text(self, _chat_id, _text, *, message_uuid=None):
            self.calls.append(message_uuid)
            if self.crash:
                self.crash = False
                raise CrashAfterProviderSuccess()
            return "om-warning"

    sender = Sender()
    database, warnings, scheduler, warning_id = _warning_delivery_fixture(
        monkeypatch, sender,
    )
    with pytest.raises(CrashAfterProviderSuccess):
        asyncio.run(scheduler._deliver_warning(warnings.pending(TEST_NOW)[0]))
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "claimed"

    _WarningDeliveryDateTime.current = TEST_NOW + timedelta(seconds=2)
    recovered = warnings.pending(_WarningDeliveryDateTime.current)
    asyncio.run(scheduler._deliver_warning(recovered[0]))

    assert sender.calls == [str(warning_id), str(warning_id)]
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "sent"


def test_retry_after_ambiguous_send_failure_reuses_same_message_uuid(monkeypatch):
    class Sender:
        def __init__(self):
            self.calls = []

        def send_text(self, _chat_id, _text, *, message_uuid=None):
            self.calls.append(message_uuid)
            if len(self.calls) == 1:
                raise FeishuSendError("ambiguous timeout", retryable=True)
            return "om-warning"

    sender = Sender()
    database, warnings, scheduler, warning_id = _warning_delivery_fixture(
        monkeypatch, sender,
    )
    asyncio.run(scheduler._deliver_warning(warnings.pending(TEST_NOW)[0]))
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "pending"
        assert row.attempt_count == 1

    _WarningDeliveryDateTime.current = TEST_NOW + timedelta(seconds=1)
    retry = warnings.pending(_WarningDeliveryDateTime.current)
    asyncio.run(scheduler._deliver_warning(retry[0]))

    assert sender.calls == [str(warning_id), str(warning_id)]
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "sent"
        assert row.attempt_count == 2


def test_warning_delivery_is_idempotent_across_crash_after_provider_success(monkeypatch):
    class CrashAfterProviderSuccess(BaseException):
        pass

    class FakeProvider:
        def __init__(self):
            self.sent_by_uuid = {}
            self.attempts = []
            self.crash = True

        def send_text(self, _chat_id, _text, *, message_uuid=None):
            self.attempts.append(message_uuid)
            message_id = self.sent_by_uuid.setdefault(
                message_uuid, f"om-{len(self.sent_by_uuid) + 1}",
            )
            if self.crash:
                self.crash = False
                raise CrashAfterProviderSuccess()
            return message_id

    provider = FakeProvider()
    database, warnings, scheduler, warning_id = _warning_delivery_fixture(
        monkeypatch, provider,
    )
    with pytest.raises(CrashAfterProviderSuccess):
        asyncio.run(scheduler._deliver_warning(warnings.pending(TEST_NOW)[0]))

    _WarningDeliveryDateTime.current = TEST_NOW + timedelta(seconds=2)
    asyncio.run(scheduler._deliver_warning(
        warnings.pending(_WarningDeliveryDateTime.current)[0]
    ))

    assert provider.attempts == [str(warning_id), str(warning_id)]
    assert provider.sent_by_uuid == {str(warning_id): "om-1"}
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "sent"


def test_claimed_warning_superseded_before_send_is_not_sent(monkeypatch):
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return TEST_NOW.astimezone(tz) if tz else TEST_NOW.replace(tzinfo=None)

    class Sender:
        sent = []

        def send_text(self, _chat_id, text, *, message_uuid=None):
            assert message_uuid is not None
            self.sent.append(text)

    class Bindings:
        def get_for_participant(self, _participant_id):
            supersede_claimed_warning(
                database, participant, warnings, warning_id, now=TEST_NOW
            )
            return {"chat_id": "oc_test"}

    monkeypatch.setattr("app.services.forecast_scheduler.datetime", FixedDateTime)

    async def scenario():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")
        nonlocal_warning_id = None
        with database.session() as session:
            row = session.query(WarningSchedule).one()
            row.target_time = TEST_NOW - timedelta(seconds=1)
            row.next_attempt_at = TEST_NOW - timedelta(seconds=1)
            row.valid_until = TEST_NOW + timedelta(minutes=10)
            row.risk_time = TEST_NOW + timedelta(minutes=20)
            nonlocal_warning_id = row.id
        return nonlocal_warning_id

    warning_id = asyncio.run(scenario())
    sender = Sender()
    scheduler = ForecastScheduler(
        coordinator=coordinator, participants=None, warnings=warnings,
        bindings=Bindings(), sender=sender, timezone_name="Asia/Shanghai",
        calendar_oauth_app_id="calendar-app",
        daily_prepare_local_time="07:30", calendar_sync_interval_seconds=999,
        warning_poll_interval_seconds=999,
    )
    asyncio.run(scheduler._deliver_warning(warnings.pending(TEST_NOW)[0]))
    assert sender.sent == []
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "pending"
        assert row.forecast_version == "new-forecast-version"
        assert row.payload_json["message"] == "NEW RED WARNING"
        assert row.claim_token is None


def test_stale_claim_cannot_mark_new_warning_sent():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.target_time = TEST_NOW - timedelta(seconds=1)
        row.next_attempt_at = TEST_NOW - timedelta(seconds=1)
        row.valid_until = TEST_NOW + timedelta(minutes=10)
        row.risk_time = TEST_NOW + timedelta(minutes=20)
        warning_id = row.id
    claimed = warnings.claim_if_current(warning_id, now=TEST_NOW)
    supersede_claimed_warning(database, participant, warnings, warning_id)
    assert warnings.finish_claim(
        warning_id, claim_token=claimed["claim_token"],
        expected_forecast_version=claimed["forecast_version"],
        sent=True, now=TEST_NOW,
    ) is False
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "pending"
        assert row.forecast_version == "new-forecast-version"
        assert row.sent_at is None


def test_stale_claim_failure_does_not_increment_new_warning_attempts():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.target_time = TEST_NOW - timedelta(seconds=1)
        row.next_attempt_at = TEST_NOW - timedelta(seconds=1)
        row.valid_until = TEST_NOW + timedelta(minutes=10)
        row.risk_time = TEST_NOW + timedelta(minutes=20)
        warning_id = row.id
    claimed = warnings.claim_if_current(warning_id, now=TEST_NOW)
    supersede_claimed_warning(database, participant, warnings, warning_id)
    assert warnings.finish_claim(
        warning_id, claim_token=claimed["claim_token"],
        expected_forecast_version=claimed["forecast_version"],
        sent=False, now=TEST_NOW, retryable=False, error_class="OldFailure",
    ) is False
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "pending"
        assert row.forecast_version == "new-forecast-version"
        assert row.attempt_count == 0
        assert row.last_error_class is None


def test_warning_episode_time_drift_dedupes_and_tier_escalates():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        return await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    first = asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.status = "sent"
        row.sent_at = TEST_NOW
        original_id = row.id
        original_risk = row.risk_time
        episode = row.episode_identity
        forecast_id = row.forecast_id
        version = row.forecast_version
    now = TEST_NOW
    drifted = [{
        "warning_identity": episode, "episode_identity": episode,
        "target_time": original_risk - timedelta(minutes=15),
        "risk_time": original_risk + timedelta(minutes=5),
        "valid_until": original_risk + timedelta(minutes=20),
        "warning_level": "2", "episode_drift_minutes": 15, "payload": {},
    }]
    diff = warnings.sync(
        participant.id, TEST_LOCAL_DATE, forecast_id=forecast_id,
        forecast_version=version, warnings=drifted, now=now,
    )
    assert diff["kept"] == 1
    with database.session() as session:
        assert session.query(WarningSchedule).count() == 1
        row = session.get(WarningSchedule, original_id)
        row.warning_level = "1"
        row.status = "sent"
    drifted[0]["warning_level"] = "3"
    warnings.sync(
        participant.id, TEST_LOCAL_DATE, forecast_id=forecast_id,
        forecast_version=version, warnings=drifted, now=now,
    )
    with database.session() as session:
        row = session.get(WarningSchedule, original_id)
        # Warning Policy v2: a delivered episode is immutable.  A tier drift
        # never revives the same audit row or bypasses durable interval/cap.
        assert row.status == "sent"
        assert "escalation" not in row.payload_json


def test_sent_episode_allows_far_later_occurrence_with_same_exact_identity():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        return await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.status = "sent"
        episode = row.episode_identity
        risk_time = row.risk_time
        forecast_id = row.forecast_id
        version = row.forecast_version
    later_risk = risk_time + timedelta(hours=3)
    warnings.sync(
        participant.id, TEST_LOCAL_DATE, forecast_id=forecast_id,
        forecast_version=version, now=TEST_NOW, warnings=[{
            "warning_identity": episode, "episode_identity": episode,
            "target_time": later_risk - timedelta(minutes=20),
            "risk_time": later_risk,
            "valid_until": later_risk + timedelta(minutes=10),
            "warning_level": "2", "episode_drift_minutes": 15,
            "payload": {},
        }],
    )
    with database.session() as session:
        rows = session.query(WarningSchedule).all()
        assert len(rows) == 2
        assert {row.status for row in rows} == {"sent", "pending"}
        assert {row.episode_identity for row in rows} == {episode}


def test_claimed_row_without_lease_is_recovered_after_migration_compatibility():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        now = TEST_NOW
        row.status = "claimed"
        row.lease_until = None
        row.target_time = now - timedelta(seconds=1)
        row.next_attempt_at = now - timedelta(seconds=1)
        row.valid_until = now + timedelta(minutes=10)
        warning_id = row.id
    due = warnings.pending(TEST_NOW)
    assert [item["id"] for item in due] == [str(warning_id)]


def test_today_context_returns_latest_forecast(monkeypatch):
    database, participant, _, _, _, _, coordinator = build_pipeline([event()])

    async def prepare():
        return await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "user_curve_request")

    generated = asyncio.run(prepare())
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return TEST_NOW.astimezone(tz) if tz else TEST_NOW.replace(tzinfo=None)

    monkeypatch.setattr("app.tools.care.datetime", FixedDateTime)
    tools = CareTools(
        profiles=type("Profiles", (), {"current": lambda self, _pid: None})(),
        observations=type(
            "Obs",
            (),
            {"recent_before": lambda self, _pid, **_kwargs: []},
        )(),
        calendar=None, tokens=None,
        timezone_name="Asia/Shanghai", forecast_coordinator=coordinator,
        forecast_snapshots=ForecastSnapshotRepository(database),
    )
    context = tools.get_today_context(
        AgentContext(
            participant_id=participant.id, participant_code="P", message_id="m",
            open_id="o", chat_id="c", agent_run_id=uuid.uuid4(),
        ), {}
    )
    assert context["latest_forecast"]["forecast_version"] == generated["forecast_version"]


def test_nonretryable_feishu_failure_is_not_hot_retried():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    class Sender:
        def send_text(self, *_args, **_kwargs):
            raise FeishuSendError("no", code=230001, retryable=False)

    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc_test"}

    async def scenario():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")
        with database.session() as session:
            row = session.query(WarningSchedule).one()
            now = TEST_NOW
            row.target_time = now - timedelta(seconds=1)
            row.next_attempt_at = now - timedelta(seconds=1)
            row.valid_until = now + timedelta(minutes=10)
        item = warnings.pending(TEST_NOW)[0]
        scheduler = ForecastScheduler(
            coordinator=coordinator, participants=None, warnings=warnings,
            bindings=Bindings(), sender=Sender(), timezone_name="Asia/Shanghai",
            calendar_oauth_app_id="calendar-app",
            daily_prepare_local_time="07:30", calendar_sync_interval_seconds=999,
            warning_poll_interval_seconds=999,
        )
        await scheduler._deliver_warning(item)

    asyncio.run(scenario())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert row.next_attempt_at is None
        assert row.last_error_code == "230001"


def test_missing_chat_binding_is_rechecked_without_consuming_send_attempt(monkeypatch):
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    class Bindings:
        available = False

        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc_test"} if self.available else None

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return TEST_NOW.astimezone(tz) if tz else TEST_NOW.replace(tzinfo=None)

    monkeypatch.setattr("app.services.forecast_scheduler.datetime", FixedDateTime)

    async def scenario():
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")
        with database.session() as session:
            row = session.query(WarningSchedule).one()
            now = TEST_NOW
            row.target_time = now - timedelta(seconds=1)
            row.next_attempt_at = now - timedelta(seconds=1)
            row.valid_until = now + timedelta(minutes=10)
        item = warnings.pending(TEST_NOW)[0]
        bindings = Bindings()
        scheduler = ForecastScheduler(
            coordinator=coordinator, participants=None, warnings=warnings,
            bindings=bindings, sender=None, timezone_name="Asia/Shanghai",
            calendar_oauth_app_id="calendar-app",
            daily_prepare_local_time="07:30", calendar_sync_interval_seconds=999,
            warning_poll_interval_seconds=999,
        )
        await scheduler._deliver_warning(item)
        with database.session() as session:
            row = session.query(WarningSchedule).one()
            assert row.status == "delivery_unavailable"
            assert row.attempt_count == 0
            row.next_attempt_at = TEST_NOW - timedelta(seconds=1)
        bindings.available = True
        await scheduler._recover_delivery_channels()

    asyncio.run(scenario())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        assert row.status == "pending"
        assert row.attempt_count == 0


def test_calendar_refresh_failure_is_persisted_and_returned_as_stale():
    database, participant, calendar, _, _, _, coordinator = build_pipeline([event()])

    async def scenario():
        fresh = await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")
        assert fresh["calendar_fresh"] is True

        async def fail(*_args):
            raise TimeoutError("calendar unavailable")

        calendar.get_events = fail
        stale = await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "periodic_poll")
        assert stale["calendar_stale"] is True
        assert stale["calendar_last_refresh_error_class"] == "TimeoutError"
        assert stale["calendar_last_refresh_success_at"] is not None

    asyncio.run(scenario())


_KEEP_WARNING_DERIVATIVE = object()


def _damage_warning_derivatives(
    database,
    forecast_id,
    *,
    selected=_KEEP_WARNING_DERIVATIVE,
    windows=_KEEP_WARNING_DERIVATIVE,
    delete_unsent_schedules=False,
):
    with database.session() as session:
        row = session.get(ForecastSnapshot, uuid.UUID(forecast_id))
        if selected is not _KEEP_WARNING_DERIVATIVE:
            output = dict(row.output_json)
            output["selected_warning_candidates"] = list(selected)
            row.output_json = output
        if windows is not _KEEP_WARNING_DERIVATIVE:
            row.warning_windows_json = list(windows)
        if delete_unsent_schedules:
            session.query(WarningSchedule).filter(
                WarningSchedule.forecast_id == row.id,
                WarningSchedule.sent_at.is_(None),
            ).delete(synchronize_session=False)


def _warning_core(snapshot):
    output = dict(snapshot["output"])
    generated_at = datetime.fromisoformat(snapshot["generated_at"])
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return {
        "id": snapshot["id"],
        "forecast_version": snapshot["forecast_version"],
        "curve": snapshot["curve"],
        "peaks": snapshot["peaks"],
        "alerts": output.get("alerts"),
        "profile_revision": output.get("profile_revision"),
        "calendar_revision": snapshot["calendar_revision"],
        "semantic_revision": snapshot["semantic_revision"],
        "observation_revision": snapshot["observation_revision"],
        "algorithm_version": snapshot["algorithm_version"],
        "generated_at": generated_at.astimezone(timezone.utc),
    }


def test_exact_cache_hit_reconciles_corrupt_warning_snapshot_and_schedule(caplog):
    database, participant, _, _, prediction, _, coordinator = build_pipeline(
        [event()]
    )
    first = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-reconcile-setup"
        )
    )
    assert len(first["output"]["alerts"]) == 1
    assert len(first["output"]["selected_warning_candidates"]) == 1
    assert len(first["warning_windows"]) == 1
    _damage_warning_derivatives(
        database,
        first["id"],
        selected=[],
        windows=[],
        delete_unsent_schedules=True,
    )

    with caplog.at_level(logging.INFO, logger="app.services.forecast_coordinator"):
        repaired = asyncio.run(
            coordinator.ensure_forecast(
                participant.id, TEST_LOCAL_DATE, "warning-reconcile-exact"
            )
        )

    assert repaired["cache_hit"] is True
    assert repaired["warning_reconciled"] is True
    assert len(repaired["output"]["alerts"]) == 1
    assert len(repaired["output"]["selected_warning_candidates"]) == 1
    assert len(repaired["warning_windows"]) == 1
    assert repaired["warning_diff"]["created"] == 1
    assert prediction.calls == 1
    assert "forecast_warning_reconciled" in caplog.text
    with database.session() as session:
        rows = session.query(WarningSchedule).all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].attempt_count == 0
        assert rows[0].sent_at is None
        assert rows[0].target_time < rows[0].risk_time
        assert rows[0].valid_until < rows[0].risk_time
        assert rows[0].next_attempt_at <= rows[0].valid_until
        assert rows[0].next_attempt_at < rows[0].risk_time


def test_consistent_exact_cache_hit_does_not_repair_or_duplicate_schedule():
    database, participant, _, _, prediction, _, coordinator = build_pipeline(
        [event()]
    )
    first = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-consistent-first"
        )
    )
    second = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-consistent-second"
        )
    )

    assert first["warning_reconciled"] is False
    assert second["cache_hit"] is True
    assert second["warning_reconciled"] is False
    assert prediction.calls == 1
    with database.session() as session:
        assert session.query(WarningSchedule).count() == 1


def test_exact_cache_hit_repairs_windows_when_selected_candidates_are_correct():
    database, participant, _, _, prediction, _, coordinator = build_pipeline(
        [event()]
    )
    first = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-window-damage-setup"
        )
    )
    _damage_warning_derivatives(database, first["id"], windows=[])

    repaired = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-window-damage-repair"
        )
    )

    assert repaired["cache_hit"] is True
    assert repaired["warning_reconciled"] is True
    assert repaired["output"]["selected_warning_candidates"] == first["output"][
        "selected_warning_candidates"
    ]
    assert repaired["warning_windows"] == first["warning_windows"]
    assert prediction.calls == 1
    with database.session() as session:
        assert session.query(WarningSchedule).filter_by(status="pending").count() == 1


def test_exact_cache_hit_rederives_selected_when_persisted_windows_still_exist():
    database, participant, _, _, prediction, _, coordinator = build_pipeline(
        [event()]
    )
    first = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-selected-damage-setup"
        )
    )
    _damage_warning_derivatives(database, first["id"], selected=[])

    repaired = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-selected-damage-repair"
        )
    )

    assert repaired["cache_hit"] is True
    assert repaired["warning_reconciled"] is True
    assert repaired["output"]["selected_warning_candidates"] == first["output"][
        "selected_warning_candidates"
    ]
    assert repaired["warning_windows"] == first["warning_windows"]
    assert prediction.calls == 1


def test_materiality_cache_hit_reconciles_persisted_warning_derivatives():
    database, participant, _, semantics, prediction, _, coordinator = build_pipeline(
        [event()]
    )
    first = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-materiality-setup"
        )
    )
    _damage_warning_derivatives(
        database,
        first["id"],
        selected=[],
        windows=[],
        delete_unsent_schedules=True,
    )
    persisted_semantic_events = [
        {
            "id": item.get("event_id"),
            "metadata": {"semantic": item.get("semantic")},
        }
        for item in first["semantic_input"]
    ]
    semantics.prepare = lambda *_args, **_kwargs: (
        persisted_semantic_events,
        "changed-but-immaterial-semantic-revision",
        "rules_only",
        [],
    )

    repaired = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-materiality-repair"
        )
    )

    assert repaired["cache_hit"] is True
    assert repaired["material_change"] is False
    assert repaired["warning_reconciled"] is True
    assert len(repaired["output"]["selected_warning_candidates"]) == 1
    assert len(repaired["warning_windows"]) == 1
    assert prediction.calls == 1
    with database.session() as session:
        assert session.query(WarningSchedule).filter_by(status="pending").count() == 1


def test_warning_reconciliation_preserves_core_forecast_fields():
    database, participant, _, _, prediction, _, coordinator = build_pipeline(
        [event()]
    )
    first = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-core-preserve-setup"
        )
    )
    before = _warning_core(first)
    _damage_warning_derivatives(
        database, first["id"], selected=[], windows=[]
    )

    repaired = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-core-preserve-repair"
        )
    )

    assert repaired["warning_reconciled"] is True
    assert _warning_core(repaired) == before
    assert prediction.calls == 1


def test_warning_reconciliation_never_reopens_or_duplicates_sent_warning():
    database, participant, _, _, prediction, _, coordinator = build_pipeline(
        [event()]
    )
    first = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-sent-preserve-setup"
        )
    )
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.status = "sent"
        row.sent_at = TEST_NOW
        original_id = row.id
    _damage_warning_derivatives(
        database, first["id"], selected=[], windows=[]
    )

    repaired = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "warning-sent-preserve-repair"
        )
    )

    assert repaired["cache_hit"] is True
    assert repaired["warning_reconciled"] is True
    assert repaired["warning_diff"]["created"] == 0
    assert prediction.calls == 1
    with database.session() as session:
        rows = session.query(WarningSchedule).all()
        assert len(rows) == 1
        assert rows[0].id == original_id
        assert rows[0].status == "sent"
        assert rows[0].sent_at.replace(tzinfo=timezone.utc) == TEST_NOW
        assert session.query(WarningSchedule).filter_by(status="pending").count() == 0


def test_warning_max_daily_sends_change_invalidates_forecast_cache():
    database, participant, _, _, prediction, _, coordinator = build_pipeline([event()])
    coordinator.warning_policy = WarningPolicy(WarningDeliveryPolicyConfig(0, 240))
    first = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "warning-policy-disabled",
    ))
    assert first["output"]["alerts"]
    assert first["output"]["selected_warning_candidates"] == []
    assert first["warning_windows"] == []

    coordinator.warning_policy = WarningPolicy(WarningDeliveryPolicyConfig(2, 240))
    second = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "warning-policy-enabled",
    ))

    assert second["cache_hit"] is False
    assert second["forecast_version"] != first["forecast_version"]
    assert len(second["output"]["selected_warning_candidates"]) == 1
    assert len(second["warning_windows"]) == 1
    assert prediction.calls == 2
    with database.session() as session:
        assert session.query(WarningSchedule).filter_by(status="pending").count() == 1


def test_warning_min_interval_change_invalidates_forecast_cache():
    _, participant, _, _, _, _, coordinator = build_pipeline([event()])

    class TwoAlertPrediction:
        model = type("Model", (), {"MODEL_VERSION": "two-alert-model-v1"})()

        def __init__(self):
            self.calls = 0

        def calculate(self, **kwargs):
            self.calls += 1
            return {
                "model_version": self.model.MODEL_VERSION,
                "local_date": kwargs["local_date"],
                "trajectory": [],
                "alerts": [
                    {"time": "10:00", "tier": 2, "episode_index": 1},
                    {"time": "12:00", "tier": 2, "episode_index": 2},
                ],
            }

    prediction = TwoAlertPrediction()
    coordinator.prediction = prediction
    coordinator.warning_policy = WarningPolicy(WarningDeliveryPolicyConfig(2, 240))
    first = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "warning-interval-240",
    ))
    assert len(first["output"]["selected_warning_candidates"]) == 1

    coordinator.warning_policy = WarningPolicy(WarningDeliveryPolicyConfig(2, 60))
    second = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "warning-interval-60",
    ))

    assert second["cache_hit"] is False
    assert second["forecast_version"] != first["forecast_version"]
    assert len(second["output"]["selected_warning_candidates"]) == 2
    assert len(second["warning_windows"]) == 2
    assert prediction.calls == 2


def test_warning_window_config_change_invalidates_forecast_cache():
    _, participant, _, _, prediction, _, coordinator = build_pipeline([event()])
    first = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "warning-window-default",
    ))
    first_window = first["warning_windows"][0]

    coordinator.warning_lead_minutes = 30
    coordinator.warning_late_grace_minutes = 5
    second = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "warning-window-changed",
    ))
    second_window = second["warning_windows"][0]

    assert second["cache_hit"] is False
    assert second["forecast_version"] != first["forecast_version"]
    assert second_window["target_time"] != first_window["target_time"]
    assert second_window["valid_until"] != first_window["valid_until"]
    assert datetime.fromisoformat(second_window["target_time"]) == (
        datetime.fromisoformat(first_window["target_time"]) - timedelta(minutes=10)
    )
    assert datetime.fromisoformat(second_window["valid_until"]) == (
        datetime.fromisoformat(first_window["valid_until"]) - timedelta(minutes=15)
    )
    assert prediction.calls == 2


def test_warning_policy_version_bump_invalidates_forecast_cache():
    _, participant, _, _, prediction, _, coordinator = build_pipeline([event()])
    first = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "warning-policy-v1",
    ))

    class WarningPolicyV2(WarningPolicy):
        POLICY_VERSION = "warning-policy-v2"

    coordinator.warning_policy = WarningPolicyV2(
        WarningDeliveryPolicyConfig(2, 240)
    )
    second = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "warning-policy-v2",
    ))

    assert second["cache_hit"] is False
    assert second["forecast_version"] != first["forecast_version"]
    assert first["output"]["warning_policy_config"]["policy_version"] == "warning-policy-v1"
    assert second["output"]["warning_policy_config"]["policy_version"] == "warning-policy-v2"
    assert prediction.calls == 2


def test_legacy_forecast_without_warning_revision_is_recomputed():
    database, participant, _, _, prediction, _, coordinator = build_pipeline([event()])
    first = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "legacy-warning-revision-setup",
    ))
    with database.session() as session:
        row = session.get(ForecastSnapshot, uuid.UUID(first["id"]))
        legacy_output = dict(row.output_json)
        legacy_output.pop("warning_revision", None)
        legacy_output.pop("warning_policy_config", None)
        row.output_json = legacy_output
        row.forecast_version = "legacy-forecast-version"

    second = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "legacy-warning-revision-refresh",
    ))

    assert second["cache_hit"] is False
    assert second["forecast_version"] != "legacy-forecast-version"
    assert second["output"]["warning_revision"]
    assert second["output"]["warning_policy_config"] == {
        "policy_version": WarningPolicy.POLICY_VERSION,
        "max_daily_sends": 2,
        "min_interval_minutes": 240,
        "lead_minutes": 20,
        "late_grace_minutes": 10,
        "episode_drift_minutes": 15,
        "care_context_schema_version": "care_context.v2",
        "care_recent_observation_max_age_minutes": 360,
        "care_message_schema_version": "care_message.v3",
        "care_intervention_policy_version": "care_intervention_policy.v3",
        "care_template_library_version": "care_template_library.v3",
        "care_jitai_version": "care-jitai.v1",
        "receptivity_model_version": "receptivity-logistic-v1",
    }
    assert prediction.calls == 2


def test_log_record_factory_redacts_feishu_query_credentials():
    install_credential_redaction()
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("redaction-test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info(
            "connect %s", "wss://example.test?access_key=secret-a&ticket=secret-b"
        )
    finally:
        logger.removeHandler(handler)
    rendered = output.getvalue()
    assert "secret-a" not in rendered
    assert "secret-b" not in rendered
    assert "access_key=[redacted]" in rendered
    assert "ticket=[redacted]" in rendered
