import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

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
    ForecastCoordinator,
    classified_calendar_events,
)
from app.services.pressure_curve_service import PressureCurveService
from helpers import memory_database, warning_repository
from services.event_lifecycle import prepare_event_instances
from services.event_semantics import DIMENSIONS, validate_course_match
from settings.visual_defaults import EVENT_COLOR_MAP


TARGET_DATE = "2030-01-15"


def _raw_event(summary="高数A", **values):
    return {
        "id": "course-event",
        "summary": summary,
        "start_time": f"{TARGET_DATE}T09:00:00+08:00",
        "end_time": f"{TARGET_DATE}T10:30:00+08:00",
        **values,
    }


def _semantic_response(*, event_type="course", task_type="course", forged=False):
    return {
        "values": {key: 0.6 for key in DIMENSIONS},
        "appraisal_score_1_10": 5.0,
        "confidence": 0.85,
        "evidence_tags": ["课程候选"],
        "reasoning_summary": "标题与候选课程一致",
        "event_classification": {
            "event_type": event_type,
            "task_type": task_type,
            "confidence": 0.92,
        },
        "course_match": {
            "matched": True,
            "canonical_name": "不存在的课程" if forged else "高等数学（A类）II",
            "code": "FAKE0001" if forged else "AMTD0034",
            "confidence": 0.87,
        },
    }


class SemanticClient:
    provider = "deepseek"
    model = "semantic-test"

    def __init__(self, response=None):
        self.response = response or _semantic_response()
        self.calls = []

    def infer(self, payload):
        self.calls.append(deepcopy(dict(payload)))
        return deepcopy(self.response)


def _preprocessor(client):
    database = memory_database()
    participant = ParticipantRepository(database).create("COURSE-SEMANTIC")
    preprocessor = EventSemanticPreprocessor(
        EventSemanticCacheRepository(database),
        client=client,
        model="semantic-test",
    )
    return database, participant, preprocessor


async def _enrich(preprocessor, participant_id, event):
    _prepared, _revision, _status, misses = preprocessor.prepare(
        participant_id, [event], consent=True
    )
    completed = 0

    async def on_complete():
        nonlocal completed
        completed += 1

    await preprocessor.enqueue(participant_id, misses, on_complete)
    await preprocessor.close()
    return completed


def test_one_api_call_caches_semantics_classification_and_course_match():
    client = SemanticClient()
    _database, participant, preprocessor = _preprocessor(client)
    event = prepare_event_instances([_raw_event()], TARGET_DATE)[0]

    assert asyncio.run(_enrich(preprocessor, participant.id, event)) == 1
    prepared, _revision, status, misses = preprocessor.prepare(
        participant.id, [event], consent=True
    )

    assert len(client.calls) == 1
    assert misses == []
    assert status == "hybrid_complete"
    assert client.calls[0]["course_catalog_context"]["candidates"]
    assert prepared[0]["event_type"] == "course"
    assert prepared[0]["course_name"] == "高等数学（A类）II"
    assert prepared[0]["metadata"]["course_name"] == "高等数学（A类）II"
    external = prepared[0]["metadata"]["semantic"]["external"]
    assert external["event_classification"]["event_type"] == "course"
    assert external["course_match"]["code"] == "AMTD0034"


def test_course_match_requires_a_real_json_boolean():
    try:
        validate_course_match(
            {"course_match": {"matched": "false"}},
            [],
        )
    except ValueError as exc:
        assert "must be boolean" in str(exc)
    else:
        raise AssertionError("string course_match.matched must be rejected")


def test_catalog_revision_changes_semantic_fingerprint():
    client = SemanticClient()
    _database, _participant, preprocessor = _preprocessor(client)
    first = prepare_event_instances([_raw_event()], TARGET_DATE)[0]
    changed = deepcopy(first)
    changed["metadata"]["classification"]["course_catalog_context"][
        "catalog_revision"
    ] = "catalog-revision-changed"

    assert preprocessor._fingerprint(first) != preprocessor._fingerprint(changed)


def test_api_course_outside_candidates_is_rejected_without_forged_identity():
    client = SemanticClient(_semantic_response(forged=True))
    _database, participant, preprocessor = _preprocessor(client)
    event = prepare_event_instances([_raw_event()], TARGET_DATE)[0]

    asyncio.run(_enrich(preprocessor, participant.id, event))
    prepared = preprocessor.prepare(participant.id, [event], consent=True)[0][0]

    assert prepared["event_type"] == "course"
    assert not prepared.get("course_name")
    course_match = prepared["metadata"]["semantic"]["external"]["course_match"]
    assert course_match["matched"] is False
    assert course_match["rejected"] == "candidate_out_of_bounds"


def test_explicit_task_type_wins_over_api_course_classification():
    client = SemanticClient()
    _database, participant, preprocessor = _preprocessor(client)
    event = prepare_event_instances(
        [_raw_event("写高数作业", event_type="task", task_type="homework")],
        TARGET_DATE,
    )[0]

    asyncio.run(_enrich(preprocessor, participant.id, event))
    prepared = preprocessor.prepare(participant.id, [event], consent=True)[0][0]

    assert prepared["event_type"] == "task"
    assert prepared["task_type"] == "homework"
    assert prepared["related_course_name"] == "高等数学（A类）II"
    assert not prepared.get("course_name")


def test_exact_catalog_course_wins_over_api_task_classification():
    client = SemanticClient(
        _semantic_response(event_type="task", task_type="homework")
    )
    _database, participant, preprocessor = _preprocessor(client)
    event = prepare_event_instances([_raw_event("线代")], TARGET_DATE)[0]

    asyncio.run(_enrich(preprocessor, participant.id, event))
    prepared = preprocessor.prepare(participant.id, [event], consent=True)[0][0]

    assert prepared["event_type"] == "course"
    assert prepared["task_type"] == "course"
    assert prepared["course_name"] == "线性代数"
    assert prepared["course_match_source"] == "catalog_alias"


def test_semantic_reclassification_finalizes_lifecycle_policy():
    response = _semantic_response(event_type="other", task_type="general")
    response["course_match"] = {
        "matched": False,
        "canonical_name": None,
        "code": None,
        "confidence": 0.0,
    }
    client = SemanticClient(response)
    _database, participant, preprocessor = _preprocessor(client)
    event = prepare_event_instances([_raw_event("校园散步")], TARGET_DATE)[0]
    assert event["lifecycle"]["completion_policy"] == "progress"

    asyncio.run(_enrich(preprocessor, participant.id, event))
    prepared = preprocessor.prepare(participant.id, [event], consent=True)[0][0]

    assert prepared["event_type"] == "other"
    assert prepared["lifecycle"]["completion_policy"] == "none"
    assert prepared["lifecycle"]["outcome_status"] == "not_applicable"


def test_final_classification_drives_course_blue_and_other_stays_gray():
    event = prepare_event_instances([_raw_event("线代")], TARGET_DATE)[0]
    presentation = classified_calendar_events([event])[0]

    assert presentation["event_type"] == "course"
    assert EVENT_COLOR_MAP[presentation["event_type"]] == ("#4169E1", "课程")
    assert EVENT_COLOR_MAP.get("other", ("#7f7f7f", "其他")) == (
        "#7f7f7f",
        "其他",
    )


def test_forecast_recompute_persists_and_returns_final_classified_events():
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create("COURSE-FORECAST")
    participant = participants.set_external_llm_consent(participant.id, allowed=True)
    client = SemanticClient()
    semantics = EventSemanticPreprocessor(
        EventSemanticCacheRepository(database),
        client=client,
        model="semantic-test",
    )

    class Calendar:
        async def get_events(self, *_args):
            return [_raw_event()]

    class Prediction:
        model = SimpleNamespace(MODEL_VERSION="classification-test-v1")

        def calculate(self, **_kwargs):
            return {
                "trajectory": [
                    {"time": "09:00", "stress_0_10": 4.0},
                    {"time": "10:00", "stress_0_10": 5.0},
                ],
                "alerts": [],
            }

    forecasts = ForecastSnapshotRepository(database)
    coordinator = ForecastCoordinator(
        participants=participants,
        profiles=ProfileRepository(database),
        observations=ObservationRepository(database),
        calendar=Calendar(),
        calendar_snapshots=CalendarSnapshotRepository(database),
        semantics=semantics,
        prediction=Prediction(),
        forecasts=forecasts,
        warnings=warning_repository(database),
        timezone_name="Asia/Shanghai",
    )

    async def scenario():
        first = await coordinator.ensure_forecast(
            participant.id, TARGET_DATE, "course-classification"
        )
        assert first["calendar_events"][0]["event_type"] == "course"
        await semantics.close()

    asyncio.run(scenario())
    latest = forecasts.latest(participant.id, datetime.fromisoformat(TARGET_DATE).date())
    saved = latest["output"]["classified_calendar_events"][0]
    assert len(client.calls) == 1
    assert saved["summary"] == "高数A"
    assert saved["event_type"] == "course"
    assert saved["course_name"] == "高等数学（A类）II"
    assert saved["course_code"] == "AMTD0034"


def test_historical_pressure_curve_uses_persisted_classification():
    database = memory_database()
    participant = ParticipantRepository(database).create("HISTORICAL-COURSE")
    target = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    classified = {
        **_raw_event("高数A"),
        "event_type": "course",
        "task_type": "course",
        "course_name": "高等数学（A类）II",
        "course_code": "AMTD0034",
    }
    forecasts = ForecastSnapshotRepository(database)
    forecasts.save(
        participant.id,
        target,
        calendar_revision="original-calendar",
        semantic_revision="semantic-v1",
        algorithm_version="model-v1",
        forecast_version="forecast-v1",
        semantic_status="hybrid_complete",
        semantic_input=[],
        curve=[
            {"time": "09:00", "stress_0_10": 4.0},
            {"time": "10:00", "stress_0_10": 5.0},
        ],
        peaks=[],
        warning_windows=[],
        output={"classified_calendar_events": [classified]},
    )
    calendars = CalendarSnapshotRepository(database)
    calendars.upsert(
        participant.id,
        target,
        revision="changed-calendar",
        events=[{**classified, "event_type": "other", "course_name": None}],
        degraded=False,
    )

    class Renderer:
        def render(self, *_args, **_kwargs):
            return b"png"

    coordinator = SimpleNamespace(forecasts=forecasts, calendar_snapshots=calendars)
    service = PressureCurveService(
        coordinator, timezone_name="Asia/Shanghai", renderer=Renderer()
    )
    view = asyncio.run(
        service.build(
            participant.id,
            target,
            reason="historical-classification",
        )
    )

    assert view.forecast["calendar_events"][0]["event_type"] == "course"
    assert view.forecast["calendar_events"][0]["course_name"] == "高等数学（A类）II"
