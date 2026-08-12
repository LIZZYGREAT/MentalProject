import asyncio
from datetime import date, datetime, timedelta, timezone
import inspect
import subprocess
import sys
import uuid

from app import main as app_main
from app.repositories import (
    CalendarSnapshotRepository,
    EventSemanticCacheRepository,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
    ProfileRepository,
    WarningScheduleRepository,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.forecast_coordinator import ForecastCoordinator, normalized_calendar_revision
from helpers import memory_database


TEST_LOCAL_DATE = date(2030, 1, 15)


class MutableCalendar:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    async def get_events(self, _participant_id, _start, _end):
        self.calls += 1
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


def event(summary="汇报", description="准备正式汇报", start="10:00", end="11:00"):
    target = TEST_LOCAL_DATE.isoformat()
    return {
        "id": "event-1", "summary": summary, "description": description,
        "start_time": f"{target}T{start}:00+08:00",
        "end_time": f"{target}T{end}:00+08:00",
    }


def build_pipeline(events, *, consent=False, client=None):
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create("FORECAST-TEST")
    if consent:
        participant = participants.set_external_llm_consent(participant.id, allowed=True)
    cache = EventSemanticCacheRepository(database)
    semantics = EventSemanticPreprocessor(
        cache, client=client, model="semantic-test-v1",
        max_concurrency=2,
    )
    prediction = FakePrediction()
    warnings = WarningScheduleRepository(database)
    calendar = MutableCalendar(events)
    coordinator = ForecastCoordinator(
        participants=participants, profiles=ProfileRepository(database),
        observations=ObservationRepository(database), calendar=calendar,
        calendar_snapshots=CalendarSnapshotRepository(database), semantics=semantics,
        prediction=prediction, forecasts=ForecastSnapshotRepository(database),
        warnings=warnings, timezone_name="Asia/Shanghai", materiality_threshold=0.03,
    )
    return database, participant, calendar, semantics, prediction, warnings, coordinator


def test_calendar_revision_is_canonical_and_detects_time_or_text_change():
    first = event()
    same_reordered = {key: first[key] for key in reversed(first)}
    assert normalized_calendar_revision([first])[0] == normalized_calendar_revision([same_reordered])[0]
    assert normalized_calendar_revision([first])[0] != normalized_calendar_revision([event(start="11:00", end="12:00")])[0]
    assert normalized_calendar_revision([first])[0] != normalized_calendar_revision([event(description="修改描述")])[0]


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
