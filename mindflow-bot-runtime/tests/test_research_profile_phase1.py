from datetime import date, datetime, timedelta, timezone

import pytest

from app.admin_web.repositories import AdminRepository
from app.contracts.research import OBSERVATION_TAXONOMY, validate_profile_v2
from app.repositories import (
    EventAppraisalFeedbackRepository,
    LearnedProfileRepository,
    ParticipantSlowStateRepository,
    ProfileRepository,
    PsychometricAssessmentRepository,
)
from helpers import memory_database, participant


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
    assert set(detail["profile_layers"]) == {
        "explicit",
        "psychometrics",
        "slow_state",
        "learned_parameters",
    }
    assert detail["profile_layers"]["psychometrics"][0]["instrument_name"] == "PSS"
    assert detail["profile_layers"]["learned_parameters"][0][
        "validation_status"
    ] == "candidate"


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

