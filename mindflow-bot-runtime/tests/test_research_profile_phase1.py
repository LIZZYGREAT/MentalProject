import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.admin_web.repositories import AdminRepository
from app.contracts.research import OBSERVATION_TAXONOMY, validate_profile_v2
from app.models import LearnedModelProfile, ParticipantSlowState
from app.repositories import (
    CalendarSnapshotRepository,
    EventAppraisalFeedbackRepository,
    EventSemanticCacheRepository,
    ForecastSnapshotRepository,
    LearnedProfileRepository,
    ObservationRepository,
    ParticipantRepository,
    ParticipantSlowStateRepository,
    ProfileRepository,
    PsychometricAssessmentRepository,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.forecast_coordinator import ForecastCoordinator
from helpers import memory_database, participant, warning_repository


NOW = datetime(2030, 1, 15, 8, 30, tzinfo=timezone.utc)


def _profile_v2() -> dict:
    return {
        "schema_version": "2.0",
        "explicit": {
            "preferred_name": {
                "value": "小岚",
                "source": "participant",
                "updated_at": NOW.isoformat(),
            },
            "preferred_recovery_methods": {
                "value": ["散步"],
                "source": "participant",
                "updated_at": NOW.isoformat(),
            },
        },
        "model_params": {},
    }


def test_profile_schema_v2_requires_field_provenance_and_keeps_legacy_compatible():
    assert validate_profile_v2({"preferred_name": "legacy"}) == {
        "preferred_name": "legacy"
    }
    with pytest.raises(ValueError, match="value/source/updated_at"):
        validate_profile_v2(
            {
                "schema_version": "2.0",
                "explicit": {"preferred_name": "小岚"},
            }
        )
    with pytest.raises(ValueError, match="top-level fields"):
        validate_profile_v2(
            {
                **_profile_v2(),
                "model_inferred_resilience": 0.8,
            }
        )
    invalid_explicit = _profile_v2()
    invalid_explicit["explicit"]["model_inferred_resilience"] = {
        "value": 0.8,
        "source": "model",
        "updated_at": NOW.isoformat(),
    }
    with pytest.raises(ValueError, match="explicit profile fields"):
        validate_profile_v2(invalid_explicit)

    database = memory_database()
    person = participant(database, "P001")
    version = ProfileRepository(database).save(person.id, _profile_v2())

    assert version == 1
    current = ProfileRepository(database).current(person.id)
    assert current["profile"]["schema_version"] == "2.0"
    assert current["profile"]["explicit"]["preferred_name"]["source"] == "participant"


def test_psychometric_history_supports_pss_and_brs_without_overwriting_versions():
    database = memory_database()
    person = participant(database, "P001")
    repository = PsychometricAssessmentRepository(database)

    repository.record(
        person.id,
        instrument_name="PSS",
        instrument_version="10-item-v1",
        language="zh-CN",
        raw_items={"q1": 2, "q2": 3},
        scores={"total": 21},
        administered_at=NOW,
        reference_period="past_month",
    )
    repository.record(
        person.id,
        instrument_name="brs",
        instrument_version="6-item-v1",
        language="zh-CN",
        raw_items={"q1": 4},
        scores={"total_mean": 3.5},
        administered_at=NOW + timedelta(days=30),
        reference_period="current",
    )

    history = repository.history(person.id)
    assert [item["instrument_name"] for item in history] == ["BRS", "PSS"]
    assert history[1]["scores"] == {"total": 21}
    with pytest.raises(ValueError, match="PSS"):
        repository.record(
            person.id,
            instrument_name="RYFF",
            instrument_version="v1",
            language="zh-CN",
            raw_items={},
            scores={},
            administered_at=NOW,
        )


def test_slow_state_and_event_appraisal_enforce_stage1_scales_and_time_semantics():
    database = memory_database()
    person = participant(database, "P001")
    slow_states = ParticipantSlowStateRepository(database)
    appraisals = EventAppraisalFeedbackRepository(database)

    slow = slow_states.record(
        person.id,
        effective_at=NOW,
        cadence="daily",
        source="rolling-profile.v1",
        values={
            "rolling_7d_stress": 6.2,
            "rolling_7d_workload": 7.1,
            "rolling_7d_energy": 4.8,
            "recent_recovery_quality": 5.0,
            "recent_sleep_debt": 3.5,
            "exam_period_flag": True,
        },
    )
    assert slow["rolling_7d_workload"] == 7.1
    assert slow["exam_period_flag"] is True

    scores = {
        "mental_demand": 8,
        "physical_demand": 2,
        "temporal_demand": 7,
        "effort": 8,
        "frustration": 6,
        "perceived_control": 4,
        "actual_stress": 7,
        "perceived_performance": 6,
    }
    appraisal = appraisals.record(
        person.id,
        event_id="calendar-event-1",
        submitted_at=NOW + timedelta(hours=2),
        **scores,
    )
    assert appraisal["actual_stress"] == 7.0
    assert appraisals.history(person.id)[0]["event_id"] == "calendar-event-1"

    with pytest.raises(ValueError, match="between 0 and 10"):
        appraisals.record(
            person.id,
            event_id="calendar-event-2",
            submitted_at=NOW,
            **{**scores, "effort": 11},
        )
    with pytest.raises(ValueError, match="timezone"):
        slow_states.record(
            person.id,
            effective_at=datetime(2030, 1, 15, 8, 30),
            cadence="weekly",
            source="rolling-profile.v1",
            values={},
        )


def test_learned_parameters_and_admin_expose_all_four_profile_layers():
    database = memory_database()
    person = participant(database, "P001")
    ProfileRepository(database).save(person.id, _profile_v2())
    PsychometricAssessmentRepository(database).record(
        person.id,
        instrument_name="PSS",
        instrument_version="10-item-v1",
        language="zh-CN",
        raw_items={"q1": 2},
        scores={"total": 18, "subscales": {}},
        administered_at=NOW,
        reference_period="past_month",
    )
    ParticipantSlowStateRepository(database).record(
        person.id,
        effective_at=NOW,
        cadence="daily",
        source="rolling-profile.v1",
        values={"rolling_7d_stress": 5.5},
    )
    learned = LearnedProfileRepository(database).save(
        person.id,
        parameters={"stress_reactivity": 1.14},
        uncertainty={"stress_reactivity": {"std_error": 0.19}},
        sample_count=32,
        day_count=14,
        confidence=0.8,
        window_start=date(2029, 12, 1),
        window_end=date(2029, 12, 14),
        model_version="ctssm-calibration-v2",
        validation_status="candidate",
    )

    assert learned["model_version"] == "ctssm-calibration-v2"
    assert learned["uncertainty"]["stress_reactivity"]["std_error"] == 0.19
    detail = AdminRepository(database).participant("P001")
    assert {
        "explicit",
        "psychometrics",
        "slow_state",
        "learned_parameters",
    } <= set(detail["profile_layers"])
    assert detail["profile_layers"]["psychometrics"][0]["instrument_name"] == "PSS"
    assert detail["profile_layers"]["explicit"]["data"] == _profile_v2()["explicit"]
    assert "model_params" not in detail["profile_layers"]["explicit"]["data"]
    assert detail["profile_layers"]["schema_version"] == "2.0"
    assert detail["profile_layers"]["legacy_compatibility"] == {
        "model_params": {}
    }
    assert detail["profile_layers"]["learned_parameters"][0][
        "validation_status"
    ] == "candidate"


@pytest.mark.parametrize(
    ("rows", "expected_stress", "expected_active_version"),
    [
        (
            [("candidate", "legacy", 41.0), ("candidate", "cal-v2", 92.0)],
            41.0,
            1,
        ),
        (
            [
                ("candidate", "legacy", 41.0),
                ("candidate", "cal-v2", 92.0),
                ("validated", "cal-v3", 63.0),
            ],
            63.0,
            3,
        ),
        (
            [("validated", "cal-v3", 63.0), ("rejected", "cal-v4", 96.0)],
            63.0,
            1,
        ),
        ([("candidate", "cal-v2", 92.0)], None, None),
    ],
)
def test_forecast_only_receives_runtime_active_learned_parameters(
    rows, expected_stress, expected_active_version
):
    database = memory_database()
    person = ParticipantRepository(database).create("P-RUNTIME-ACTIVE")
    learned = LearnedProfileRepository(database)
    for status, model_version, stress in rows:
        learned.save(
            person.id,
            parameters={"S_star_init": stress},
            uncertainty=(
                {"S_star_init": {"std_error": 0.2}}
                if status == "validated" else {}
            ),
            sample_count=20,
            day_count=10,
            confidence=0.8,
            window_start=date(2029, 12, 1),
            window_end=date(2029, 12, 14),
            model_version=model_version,
            validation_status=status,
        )

    class Calendar:
        async def get_events(self, *_args):
            return []

    class Model:
        MODEL_VERSION = "gate-test-v1"

    class Prediction:
        model = Model()

        def __init__(self):
            self.profile = None

        def calculate(self, **kwargs):
            self.profile = kwargs["profile"]
            return {
                "model_version": self.model.MODEL_VERSION,
                "local_date": kwargs["local_date"],
                "trajectory": [{"time": "12:00", "stress_0_10": 5.0}],
                "alerts": [],
                "received_model_params": dict(
                    kwargs["profile"].get("model_params") or {}
                ),
            }

    prediction = Prediction()
    coordinator = ForecastCoordinator(
        participants=ParticipantRepository(database),
        profiles=ProfileRepository(database),
        observations=ObservationRepository(database),
        calendar=Calendar(),
        calendar_snapshots=CalendarSnapshotRepository(database),
        semantics=EventSemanticPreprocessor(
            EventSemanticCacheRepository(database),
            client=None,
            model="rules-only",
        ),
        prediction=prediction,
        forecasts=ForecastSnapshotRepository(database),
        warnings=warning_repository(database),
        timezone_name="Asia/Shanghai",
        learned_profiles=learned,
    )
    forecast = asyncio.run(
        coordinator.ensure_forecast(
            person.id, date(2030, 1, 15), "stage1_gate_test"
        )
    )

    passed_params = prediction.profile["model_params"]
    persisted_params = forecast["output"]["received_model_params"]
    if expected_stress is None:
        assert passed_params == {}
        assert persisted_params == {}
    else:
        assert passed_params["S_star_init"] == expected_stress
        assert persisted_params["S_star_init"] == expected_stress
    assert forecast["output"]["profile_layers"]["learned_version"] == (
        expected_active_version
    )


def test_learned_repository_latest_and_runtime_active_are_distinct():
    database = memory_database()
    person = participant(database, "P001")
    learned = LearnedProfileRepository(database)
    legacy = learned.save(
        person.id,
        parameters={"S_star_init": 41.0},
        sample_count=10,
        day_count=5,
        confidence=0.5,
        window_start=date(2029, 1, 1),
        window_end=date(2029, 1, 5),
        model_version="legacy",
        validation_status="candidate",
    )
    candidate = learned.save(
        person.id,
        parameters={"S_star_init": 92.0},
        sample_count=20,
        day_count=10,
        confidence=0.7,
        window_start=date(2029, 2, 1),
        window_end=date(2029, 2, 10),
        model_version="cal-v2",
        validation_status="candidate",
    )

    assert learned.latest(person.id)["version"] == candidate["version"]
    assert learned.runtime_active(person.id)["version"] == legacy["version"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sample_count": -1}, "sample_count"),
        ({"day_count": -1}, "day_count"),
        ({"confidence": 1.1}, "confidence"),
        ({"window_start": date(2030, 2, 1)}, "window_start"),
        ({"source": ""}, "source"),
        ({"model_version": ""}, "model_version"),
        ({"validation_status": "active"}, "validation_status"),
        (
            {"validation_status": "validated", "uncertainty": {}},
            "uncertainty",
        ),
    ],
)
def test_learned_repository_fails_closed_on_invalid_audit_data(overrides, message):
    database = memory_database()
    person = participant(database, "P001")
    values = {
        "parameters": {"stress_reactivity": 1.1},
        "uncertainty": {"stress_reactivity": {"std_error": 0.2}},
        "sample_count": 20,
        "day_count": 10,
        "confidence": 0.8,
        "window_start": date(2030, 1, 1),
        "window_end": date(2030, 1, 14),
        "source": "calibration.v2",
        "model_version": "cal-v2",
        "validation_status": "candidate",
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        LearnedProfileRepository(database).save(person.id, **values)


def test_sqlite_schema_enforces_learned_and_slow_state_constraints():
    database = memory_database()
    person = participant(database, "P001")
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(
                ParticipantSlowState(
                    participant_id=person.id,
                    effective_at=NOW,
                    cadence="monthly",
                    rolling_7d_stress=11,
                    source="invalid-fixture",
                )
            )
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(
                LearnedModelProfile(
                    participant_id=person.id,
                    version=1,
                    parameters_json={"stress_reactivity": 1.0},
                    uncertainty_json={},
                    source="invalid-fixture",
                    model_version="cal-v2",
                    validation_status="active",
                    sample_count=-1,
                    day_count=1,
                    confidence=1.2,
                    window_start=date(2030, 2, 1),
                    window_end=date(2030, 1, 1),
                )
            )


def test_observation_taxonomy_never_classifies_daily_review_as_momentary():
    assert OBSERVATION_TAXONOMY["momentary_state"]["types"] == ("checkin",)
    assert OBSERVATION_TAXONOMY["retrospective_state"]["types"] == (
        "daily_review",
    )
    assert OBSERVATION_TAXONOMY["momentary_state"]["time_field"] == "observed_at"
    assert (
        OBSERVATION_TAXONOMY["momentary_state"]["knowledge_time_field"]
        == "created_at"
    )
