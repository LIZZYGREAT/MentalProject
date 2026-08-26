import asyncio
from datetime import date, datetime, timedelta, timezone
import inspect
import subprocess
import sys
import threading
import uuid

import pytest
from sqlalchemy import select

from app import main as app_main
from app.models import WarningSchedule
from app.repositories import (
    CalendarSnapshotRepository,
    EventSemanticCacheRepository,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
    ProfileRepository,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.forecast_coordinator import (
    CalendarRefreshPendingError,
    ForecastCoordinator,
    normalized_calendar_revision,
)
from app.services.prediction_service import PredictionService
from helpers import memory_database, warning_repository
from mindflow_core.assessment import AssessmentModel


TEST_LOCAL_DATE = date(2030, 1, 15)


class MutableCalendar:
    def __init__(self, events):
        self.events = events
        self.calls = 0
        self.error = None

    async def get_events(self, _participant_id, _start, _end):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [dict(item) for item in self.events]


class FakeModel:
    MODEL_VERSION = "fake-algorithm-v1"


class FakePrediction:
    model = FakeModel()

    def __init__(self):
        self.calls = 0

    def calculate(self, **kwargs):
        self.calls += 1
        return {
            "model_version": self.model.MODEL_VERSION,
            "local_date": kwargs["local_date"],
            "trajectory": [
                {"time": "12:00", "stress_0_10": 6.0},
                {"time": "13:00", "stress_0_10": 8.0},
            ],
            "alerts": [
                {"time": "23:50", "tier": 2, "message": "测试预警"}
            ] if kwargs["calendar_events"] else [],
        }


def event(
    summary="汇报", description="准备正式汇报", start="10:00", end="11:00",
    event_id="event-1",
):
    target = TEST_LOCAL_DATE.isoformat()
    return {
        "id": event_id, "summary": summary, "description": description,
        "start_time": f"{target}T{start}:00+08:00",
        "end_time": f"{target}T{end}:00+08:00",
    }


def build_pipeline(
    events, *, consent=False, client=None, semantic_max_concurrency=2,
):
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create("FORECAST-TEST")
    if consent:
        participant = participants.set_external_llm_consent(participant.id, allowed=True)
    cache = EventSemanticCacheRepository(database)
    semantics = EventSemanticPreprocessor(
        cache, client=client, model="semantic-test-v1",
        max_concurrency=semantic_max_concurrency,
    )
    prediction = FakePrediction()
    warnings = warning_repository(database)
    calendar = MutableCalendar(events)
    coordinator = ForecastCoordinator(
        participants=participants, profiles=ProfileRepository(database),
        observations=ObservationRepository(database), calendar=calendar,
        calendar_snapshots=CalendarSnapshotRepository(database), semantics=semantics,
        prediction=prediction, forecasts=ForecastSnapshotRepository(database),
        warnings=warnings, timezone_name="Asia/Shanghai", materiality_threshold=0.03,
    )
    return database, participant, calendar, semantics, prediction, warnings, coordinator


def test_real_forecast_pipeline_accepts_timezone_aware_calendar_snapshot():
    raw_event = {
        "id": "real-event", "summary": "准备正式汇报",
        "description": "完成项目汇报材料", "event_type": "task",
        "start_time": "2030-01-15T07:00:00+00:00",
        "end_time": "2030-01-15T08:00:00+00:00",
    }
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create("REAL-FORECAST-TEST")
    calendar = MutableCalendar([raw_event])
    warnings = warning_repository(database)
    prediction = PredictionService(AssessmentModel("Asia/Shanghai"))
    coordinator = ForecastCoordinator(
        participants=participants, profiles=ProfileRepository(database),
        observations=ObservationRepository(database), calendar=calendar,
        calendar_snapshots=CalendarSnapshotRepository(database),
        semantics=EventSemanticPreprocessor(
            EventSemanticCacheRepository(database), client=None, model="rules-only"
        ),
        prediction=prediction, forecasts=ForecastSnapshotRepository(database),
        warnings=warnings, timezone_name="Asia/Shanghai",
    )

    forecast = asyncio.run(coordinator.ensure_forecast(
        participant.id, TEST_LOCAL_DATE, "real_assessment_regression"
    ))

    assert forecast["valid"] is True
    assert forecast["curve"]
    assert forecast["semantic_status"] == "rules_only"
    assert forecast["output"]["calendar_event_count"] == 1
    assert any(
        "准备正式汇报" in point["current_events"]
        for point in forecast["curve"]
        if point["time"] == "15:00"
    )
    snapshot = CalendarSnapshotRepository(database).get(
        participant.id, TEST_LOCAL_DATE
    )
    assert snapshot["events"][0]["start_time"] == raw_event["start_time"]


def test_calendar_revision_is_canonical_and_detects_time_or_text_change():
    first = event()
    same_reordered = {key: first[key] for key in reversed(first)}
    assert normalized_calendar_revision([first])[0] == normalized_calendar_revision([same_reordered])[0]
    assert normalized_calendar_revision([first])[0] != normalized_calendar_revision([event(start="11:00", end="12:00")])[0]
    assert normalized_calendar_revision([first])[0] != normalized_calendar_revision([event(description="修改描述")])[0]


def test_calendar_mutation_timeout_fails_closed_until_readback_succeeds():
    database, participant, calendar, _semantics, _prediction, warnings, coordinator = (
        build_pipeline([event()])
    )
    forecasts = ForecastSnapshotRepository(database)
    calendars = CalendarSnapshotRepository(database)
    first = asyncio.run(
        coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "initial")
    )
    assert first["valid"] is True

    forecasts.invalidate_for_calendar_mutation(
        warnings,
        participant.id,
        TEST_LOCAL_DATE,
        reason="calendar_update_event",
    )
    calendar.error = TimeoutError("provider timeout")
    with pytest.raises(CalendarRefreshPendingError):
        asyncio.run(
            coordinator.ensure_forecast(
                participant.id, TEST_LOCAL_DATE, "calendar_update_event"
            )
        )

    assert forecasts.latest(participant.id, TEST_LOCAL_DATE) is None
    pending = calendars.get(participant.id, TEST_LOCAL_DATE)
    assert pending["snapshot_state"] == "mutation_refresh_pending"
    assert len(pending["events"]) == 1
    with database.session() as session:
        statuses = session.execute(select(WarningSchedule.status)).scalars().all()
    assert statuses and set(statuses) == {"cancelled"}

    calendar.error = None
    calendar.events = [event(summary="更新后的日程", event_id="event-2")]
    recovered = asyncio.run(
        coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "calendar_refresh_retry"
        )
    )
    assert recovered["valid"] is True
    assert calendars.get(participant.id, TEST_LOCAL_DATE)["snapshot_state"] == "current"
    assert recovered["calendar_events"][0]["summary"] == "更新后的日程"


def test_ordinary_provider_timeout_keeps_stable_snapshot_degraded_fallback():
    database, participant, calendar, _semantics, _prediction, _warnings, coordinator = (
        build_pipeline([event()])
    )
    asyncio.run(coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "initial"))
    calendar.error = TimeoutError("provider timeout")

    degraded = asyncio.run(
        coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "scheduled_retry")
    )

    assert degraded["valid"] is True
    assert degraded["calendar_degraded"] is True
    snapshot = CalendarSnapshotRepository(database).get(
        participant.id, TEST_LOCAL_DATE
    )
    assert snapshot["snapshot_state"] == "provider_degraded"
    assert len(snapshot["events"]) == 1


def test_semantic_fingerprint_reuses_time_only_update_but_changes_duration_or_text():
    database = memory_database()
    cache = EventSemanticCacheRepository(database)
    preprocessor = EventSemanticPreprocessor(cache, client=None, model="m")
    base = event()
    moved = event(start="11:00", end="12:00")
    longer = event(start="11:00", end="13:00")
    changed = event(description="新的主观描述")
    assert preprocessor._fingerprint(base) == preprocessor._fingerprint(moved)
    assert preprocessor._fingerprint(base) != preprocessor._fingerprint(longer)
    assert preprocessor._fingerprint(base) != preprocessor._fingerprint(changed)


def test_semantic_fingerprint_uses_same_normalized_payload_as_api():
    class Client:
        provider = "deepseek"
        model = "fake"

        def infer(self, _payload):
            raise AssertionError("not called")

    summary = "a" * 160 + "first-tail" + "x" * 31
    changed = "a" * 160 + "other-tail" + "x" * 31
    _, participant, _, semantics, _, _, _ = build_pipeline(
        [], consent=True, client=Client()
    )
    first = event(summary=summary, end="12:30")
    second = event(summary=changed, end="12:30")
    first_miss = semantics.prepare(participant.id, [first], consent=True)[3][0]
    second_miss = semantics.prepare(participant.id, [second], consent=True)[3][0]
    assert len(first_miss["event"]["summary"]) == 200
    assert first_miss["event"]["duration_minutes"] == 150.0
    assert first_miss["fingerprint"] == semantics._fingerprint(first_miss["event"])
    assert second_miss["fingerprint"] == semantics._fingerprint(second_miss["event"])
    assert first_miss["fingerprint"] != second_miss["fingerprint"]


def test_no_consent_is_rules_only_and_never_queues_external_work():
    class Client:
        provider = "deepseek"
        model = "fake"

    database, participant, _, semantics, _, _, _ = build_pipeline([event()], client=Client())
    prepared, _revision, status, misses = semantics.prepare(
        participant.id, [event()], consent=False
    )
    assert status == "rules_only"
    assert misses == []
    semantic = prepared[0]["metadata"]["semantic"]
    assert semantic["source"] == "rules"
    assert semantic["external"] is None


def test_external_enrichment_is_single_flight_durable_and_identity_free():
    class Client:
        provider = "deepseek"
        model = "fake"

        def __init__(self):
            self.calls = 0
            self.payloads = []

        def infer(self, payload):
            self.calls += 1
            self.payloads.append(dict(payload))
            return {
                "values": {
                    "difficulty": 0.7, "cognitive_demand": 0.7, "stakes": 0.6,
                    "time_pressure": 0.5, "social_evaluation": 0.5,
                    "uncontrollability": 0.3, "novelty": 0.3,
                    "expected_effort": 0.7, "uncertainty": 0.3, "unfinished": 0.2,
                },
                "appraisal_score_1_10": 5.0,
                "confidence": 0.8, "evidence_tags": ["汇报"],
                "reasoning_summary": "正式汇报具有客观评价属性",
            }

    client = Client()
    _, participant, _, semantics, _, _, _ = build_pipeline(
        [event()], consent=True, client=client
    )

    async def scenario():
        _events, _revision, _status, misses = semantics.prepare(
            participant.id, [event()], consent=True
        )
        completed = 0

        async def done():
            nonlocal completed
            completed += 1

        await asyncio.gather(
            semantics.enqueue(participant.id, misses, done),
            semantics.enqueue(participant.id, misses, done),
        )
        await asyncio.sleep(0.1)
        prepared, _revision, status, second_misses = semantics.prepare(
            participant.id, [event()], consent=True
        )
        assert client.calls == 1
        assert second_misses == []
        assert status == "hybrid_complete"
        assert prepared[0]["metadata"]["semantic"]["source"] == "hybrid"
        assert completed == 1
        forbidden = {"participant_id", "participant_code", "open_id", "chat_id", "access_token", "refresh_token"}
        assert not forbidden.intersection(client.payloads[0])

    asyncio.run(scenario())


def test_invalid_external_response_opens_circuit_and_rules_remain_available():
    class BrokenClient:
        provider = "deepseek"
        model = "fake"

        def __init__(self):
            self.calls = 0

        def infer(self, _payload):
            self.calls += 1
            return {"invalid": True}

    client = BrokenClient()
    _, participant, _, semantics, _, _, _ = build_pipeline(
        [event()], consent=True, client=client
    )

    async def scenario():
        prepared, _revision, status, misses = semantics.prepare(
            participant.id, [event()], consent=True
        )
        await semantics.enqueue(participant.id, misses, lambda: asyncio.sleep(0))
        await asyncio.sleep(0.1)
        await semantics.enqueue(participant.id, misses, lambda: asyncio.sleep(0))
        await asyncio.sleep(0.05)
        assert client.calls == 1
        assert status == "rules_only"
        assert prepared[0]["metadata"]["semantic"]["values"]

    asyncio.run(scenario())


def test_stale_semantic_miss_does_not_repeat_api_call():
    class BlockingClient:
        provider = "deepseek"
        model = "fake"

        def __init__(self):
            self.calls = 0
            self.started = threading.Event()
            self.release = threading.Event()

        def infer(self, _payload):
            self.calls += 1
            self.started.set()
            self.release.wait()
            return {
                "values": {key: 0.6 for key in (
                    "difficulty", "cognitive_demand", "stakes", "time_pressure",
                    "social_evaluation", "uncontrollability", "novelty",
                    "expected_effort", "uncertainty", "unfinished",
                )},
                "appraisal_score_1_10": 5.0,
                "confidence": 0.8,
                "evidence_tags": [],
                "reasoning_summary": "ok",
            }

    client = BlockingClient()
    item = event(summary="stale miss")
    _, participant, _, semantics, _, _, _ = build_pipeline(
        [item], consent=True, client=client, semantic_max_concurrency=1
    )

    async def scenario():
        first_miss = semantics.prepare(participant.id, [item], consent=True)[3]
        first_completed = 0
        second_completed = 0

        async def first_done():
            nonlocal first_completed
            first_completed += 1

        async def second_done():
            nonlocal second_completed
            second_completed += 1

        await semantics.enqueue(participant.id, first_miss, first_done)
        assert await asyncio.to_thread(client.started.wait, 2)
        stale_miss = semantics.prepare(participant.id, [item], consent=True)[3]
        assert len(stale_miss) == 1
        client.release.set()
        while semantics._inflight or semantics._completion_tasks:
            await asyncio.sleep(0.01)
        await semantics.enqueue(participant.id, stale_miss, second_done)
        await semantics.close()
        assert first_completed == 1
        assert second_completed == 1
        assert semantics.prepare(participant.id, [item], consent=True)[3] == []

    asyncio.run(scenario())
    assert client.calls == 1


def test_corrupt_cache_does_not_block_real_api_repair():
    class Client:
        provider = "deepseek"
        model = "fake"

        def __init__(self):
            self.calls = 0

        def infer(self, _payload):
            self.calls += 1
            return {
                "values": {key: 0.7 for key in (
                    "difficulty", "cognitive_demand", "stakes", "time_pressure",
                    "social_evaluation", "uncontrollability", "novelty",
                    "expected_effort", "uncertainty", "unfinished",
                )},
                "appraisal_score_1_10": 5.0,
                "confidence": 0.8,
                "evidence_tags": [],
                "reasoning_summary": "repaired",
            }

    client = Client()
    item = event(summary="corrupt cache")
    _, participant, _, semantics, _, _, _ = build_pipeline(
        [item], consent=True, client=client
    )
    fingerprint = semantics._fingerprint(item)
    semantics.cache.put(
        participant.id, fingerprint,
        {"external": {"objective_semantics": {"difficulty": 0.5}}},
        schema_version="event-semantics-v2", prompt_version="event-semantics-prompt-v2",
        model="semantic-test-v1",
    )

    async def scenario():
        misses = semantics.prepare(participant.id, [item], consent=True)[3]
        assert len(misses) == 1
        await semantics.enqueue(participant.id, misses, lambda: asyncio.sleep(0))
        await semantics.close()
        assert semantics.prepare(participant.id, [item], consent=True)[3] == []

    asyncio.run(scenario())
    assert client.calls == 1


def test_circuit_recheck_after_semaphore_stops_queued_request_storm():
    class FailingClient:
        provider = "deepseek"
        model = "fake"

        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()
            self.first_batch = threading.Barrier(2)

        def infer(self, _payload):
            with self.lock:
                self.calls += 1
            self.first_batch.wait()
            raise RuntimeError("429")

    events = [
        event(summary=f"任务 {index}", event_id=f"event-{index}")
        for index in range(20)
    ]
    client = FailingClient()
    _, participant, _, semantics, _, _, _ = build_pipeline(
        events, consent=True, client=client
    )

    async def scenario():
        prepared, _, status, misses = semantics.prepare(
            participant.id, events, consent=True
        )
        completed = 0

        async def done():
            nonlocal completed
            completed += 1

        await semantics.enqueue(participant.id, misses, done)
        await semantics.close()
        assert status == "rules_only"
        assert all(item["metadata"]["semantic"]["source"] == "rules" for item in prepared)
        assert completed == 0
        assert len(semantics.prepare(participant.id, events, consent=True)[3]) == 20

    asyncio.run(scenario())
    assert client.calls == 2


def test_completion_watcher_absorbs_new_fingerprint_without_duplicate_callback():
    class BlockingClient:
        provider = "deepseek"
        model = "fake"

        def __init__(self):
            self.calls = []
            self.started_a = threading.Event()
            self.release_a = threading.Event()

        def infer(self, payload):
            self.calls.append(payload["summary"])
            if payload["summary"] == "A":
                self.started_a.set()
                self.release_a.wait()
            return {
                "values": {key: 0.8 for key in (
                    "difficulty", "cognitive_demand", "stakes", "time_pressure",
                    "social_evaluation", "uncontrollability", "novelty",
                    "expected_effort", "uncertainty", "unfinished",
                )},
                "appraisal_score_1_10": 6.0,
                "confidence": 0.8,
                "evidence_tags": [],
                "reasoning_summary": "ok",
            }

    event_a = event(summary="A", event_id="event-a")
    event_b = event(summary="B", event_id="event-b")
    client = BlockingClient()
    _, participant, _, semantics, _, _, _ = build_pipeline(
        [event_a], consent=True, client=client, semantic_max_concurrency=1
    )

    async def scenario():
        completed = 0

        async def done():
            nonlocal completed
            completed += 1

        misses_a = semantics.prepare(participant.id, [event_a], consent=True)[3]
        await semantics.enqueue(
            participant.id, misses_a, done,
            completion_key=(participant.id, TEST_LOCAL_DATE),
        )
        assert await asyncio.to_thread(client.started_a.wait, 2)
        misses_ab = semantics.prepare(
            participant.id, [event_a, event_b], consent=True
        )[3]
        await semantics.enqueue(
            participant.id, misses_ab, done,
            completion_key=(participant.id, TEST_LOCAL_DATE),
        )
        client.release_a.set()
        await semantics.close()
        prepared, _, status, misses = semantics.prepare(
            participant.id, [event_a, event_b], consent=True
        )
        assert misses == []
        assert status == "hybrid_complete"
        assert all(item["metadata"]["semantic"]["source"] == "hybrid" for item in prepared)
        assert completed == 1

    asyncio.run(scenario())
    assert sorted(client.calls) == ["A", "B"]


def test_dynamic_calendar_addition_is_enriched_and_latest_forecast_uses_it():
    class BlockingClient:
        provider = "deepseek"
        model = "fake"

        def __init__(self):
            self.calls = []
            self.started_a = threading.Event()
            self.release_a = threading.Event()

        def infer(self, payload):
            self.calls.append(payload["summary"])
            if payload["summary"] == "A":
                self.started_a.set()
                self.release_a.wait()
            return {
                "values": {
                    "difficulty": 0.9, "cognitive_demand": 0.9, "stakes": 0.8,
                    "time_pressure": 0.8, "social_evaluation": 0.7,
                    "uncontrollability": 0.6, "novelty": 0.6,
                    "expected_effort": 0.9, "uncertainty": 0.7, "unfinished": 0.6,
                },
                "appraisal_score_1_10": 2.0,
                "confidence": 0.9,
                "evidence_tags": [],
                "reasoning_summary": "material enrichment",
            }

    event_a = event(summary="A", event_id="event-a")
    event_b = event(summary="B", event_id="event-b", start="12:00", end="13:00")
    client = BlockingClient()
    database, participant, calendar, semantics, _, _, coordinator = build_pipeline(
        [event_a], consent=True, client=client, semantic_max_concurrency=1
    )

    async def scenario():
        first = await coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "daily_prepare"
        )
        assert await asyncio.to_thread(client.started_a.wait, 2)
        calendar.events = [event_a, event_b]
        second = await coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "user_curve_request"
        )
        assert second["calendar_revision"] != first["calendar_revision"]
        client.release_a.set()
        await semantics.close()

    asyncio.run(scenario())
    latest = ForecastSnapshotRepository(database).latest(
        participant.id, TEST_LOCAL_DATE
    )
    prepared, _, status, misses = semantics.prepare(
        participant.id, [event_a, event_b], consent=True
    )
    assert sorted(client.calls) == ["A", "B"]
    assert misses == []
    assert status == "hybrid_complete"
    assert {item["event_id"] for item in latest["semantic_input"]} == {"event-a", "event-b"}
    assert all(
        item["metadata"]["semantic"]["source"] == "hybrid"
        for item in prepared
    )


def test_on_demand_forecast_is_immediate_rules_baseline_and_fast_path():
    _, participant, calendar, _, prediction, _, coordinator = build_pipeline([event()])

    async def scenario():
        first = await coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "user_curve_request"
        )
        second = await coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "user_curve_request"
        )
        assert not first["cache_hit"]
        assert second["cache_hit"]
        assert first["semantic_status"] == "rules_only"
        assert first["curve"]
        assert prediction.calls == 1
        assert calendar.calls == 2  # bounded freshness check on every user request

    asyncio.run(scenario())


def test_create_delete_and_time_only_update_invalidate_forecast_not_semantic():
    _, participant, calendar, semantics, prediction, warnings, coordinator = build_pipeline([event()])

    async def scenario():
        first = await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")
        first_fingerprint = first["semantic_input"][0]["semantic"]["fingerprint"]
        calendar.events = [event(start="11:00", end="12:00")]
        moved = await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "periodic_poll")
        assert moved["forecast_version"] != first["forecast_version"]
        assert moved["semantic_input"][0]["semantic"]["fingerprint"] == first_fingerprint
        calendar.events = []
        deleted = await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "periodic_poll")
        assert deleted["forecast_version"] != moved["forecast_version"]
        assert prediction.calls == 3
        assert semantics._inflight == {}
        assert deleted["warning_diff"]["cancelled"] >= 1

    asyncio.run(scenario())


def test_forecast_pipeline_reactivates_original_snapshot_after_a_b_a_change():
    original_event = event(description="calendar-state-a")
    changed_event = event(description="calendar-state-b")
    database, participant, calendar, _, _, _, coordinator = build_pipeline(
        [original_event]
    )

    async def scenario():
        first = await coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "daily_prepare"
        )
        calendar.events = [changed_event]
        second = await coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "periodic_poll"
        )
        calendar.events = [original_event]
        third = await coordinator.ensure_forecast(
            participant.id, TEST_LOCAL_DATE, "periodic_poll"
        )
        return first, second, third

    first, second, third = asyncio.run(scenario())
    latest = ForecastSnapshotRepository(database).latest(
        participant.id, TEST_LOCAL_DATE
    )

    assert second["forecast_version"] != first["forecast_version"]
    assert third["forecast_version"] == first["forecast_version"]
    assert third["id"] == first["id"]
    assert third["cache_hit"] is False
    assert latest["id"] == first["id"]


def test_warning_is_durable_deduped_and_stale_forecast_cannot_send():
    database, participant, calendar, _, _, warnings, coordinator = build_pipeline([event()])

    async def scenario():
        first = await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")
        with database.session() as session:
            from app.models import WarningSchedule
            row = session.query(WarningSchedule).one()
            warning_id = row.id
        calendar.events = [event(description="语义发生变化")]
        await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "periodic_poll")
        # Old forecast was invalidated; a stale row cannot be claimed/sent.
        old_row = None
        with database.session() as session:
            from app.models import WarningSchedule
            old_row = session.get(WarningSchedule, warning_id)
            old_row.forecast_version = first["forecast_version"]
            old_row.forecast_id = uuid.UUID(first["id"])
            old_row.status = "pending"
        assert warnings.claim_if_current(warning_id) is None

    asyncio.run(scenario())


def test_prediction_and_spawn_import_paths_are_network_and_heavy_import_free():
    import algorithm.dynamic_state_model as dynamic_state_model

    source = inspect.getsource(dynamic_state_model)
    assert "assess_event_semantics" not in source
    assert "requests" not in source
    assert "snownlp" not in source.lower()
    # app.main itself must stay safe for a fresh spawned receiver import.
    main_source = inspect.getsource(app_main)
    assert main_source.index("async def run") < main_source.index("from app.bootstrap import")
    child = subprocess.run(
        [sys.executable, "-c", "import app.main,sys; print(int('mindflow_core.assessment' in sys.modules)); print(int('algorithm.dynamic_state_model' in sys.modules))"],
        check=True, capture_output=True, text=True,
    )
    assert child.stdout.strip().splitlines() == ["0", "0"]


def test_recoverable_dispatcher_starts_before_durable_queue_restore():
    source = inspect.getsource(app_main.run)
    assert source.index('worker.run_forever()') < source.index('events.recoverable()')


def test_subthreshold_semantic_change_keeps_persisted_forecast_fast_path():
    _, participant, _, _, prediction, _, coordinator = build_pipeline([event()])

    async def scenario():
        first = await coordinator.ensure_forecast(participant.id, TEST_LOCAL_DATE, "daily_prepare")
        semantic_events = []
        for item in first["semantic_input"]:
            semantic = dict(item["semantic"])
            semantic["values"] = dict(semantic["values"])
            semantic["values"]["difficulty"] += 0.001
            semantic_events.append({
                "id": item["event_id"], "metadata": {"semantic": semantic}
            })
        assert coordinator._semantic_input_delta(first["semantic_input"], semantic_events) < 0.03
        assert prediction.calls == 1

    asyncio.run(scenario())
