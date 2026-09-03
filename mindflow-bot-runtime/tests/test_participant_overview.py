from datetime import date, datetime, timezone

from starlette.testclient import TestClient

from app.admin_web.main import create_app
from app.models import LearnedModelProfile, Participant, ParticipantProfile, ParticipantSlowState
from app.repositories import ForecastSnapshotRepository, ObservationRepository
from app.services.participant_overview import ParticipantOverviewService
from helpers import memory_database, participant
from test_admin_web import login, settings


def _seed_full():
    database = memory_database()
    user = participant(database, "P002")
    with database.session() as session:
        session.add(
            ParticipantProfile(
                participant_id=user.id,
                version=2,
                profile_json={"schema_version": "2.0", "explicit": {"support": "brief"}},
            )
        )
        session.add(
            ParticipantSlowState(
                participant_id=user.id,
                effective_at=datetime(2026, 9, 3, 0, tzinfo=timezone.utc),
                cadence="daily",
                rolling_7d_stress=6.2,
                rolling_7d_workload=7.0,
                rolling_7d_energy=4.8,
                recent_recovery_quality=6.5,
                recent_sleep_debt=2.0,
                exam_period_flag=False,
                source="slow-state.v1",
            )
        )
        session.add(
            LearnedModelProfile(
                participant_id=user.id,
                version=4,
                parameters_json={"stress_reactivity_i": 0.9, "S_star_i": 5.2},
                uncertainty_json={"stress_reactivity_i": {"standard_error": 0.1}},
                source="stage5",
                model_version="stress-ctssm.m3",
                validation_status="validated",
                sample_count=35,
                day_count=14,
                confidence=0.81,
                window_start=date(2026, 8, 20),
                window_end=date(2026, 9, 2),
            )
        )
    repo = ForecastSnapshotRepository(database)
    for offset, workload in enumerate((0.2, 0.5, 0.8)):
        day = date(2026, 9, 1 + offset)
        curve = [
            {"time": "08:00", "stress_0_10": 4 + offset, "workload": workload},
            {"time": "16:00", "stress_0_10": 7 + offset / 2, "workload": workload},
        ]
        repo.save(
            user.id,
            day,
            calendar_revision=f"c-{offset}",
            semantic_revision=f"s-{offset}",
            observation_revision=f"o-{offset}",
            algorithm_version="forecast.v4",
            forecast_version=f"forecast-{offset}",
            semantic_status="complete",
            semantic_input=[],
            curve=curve,
            peaks=[],
            warning_windows=[],
            output={"model_family": "stress-ctssm.m3", "active_states": ["S", "V", "F"]},
        )
    ObservationRepository(database).add(
        user.id,
        "instant_checkin",
        {"stress_0_10": 6.4, "energy_0_10": 4.2},
        observed_at=datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
        source_message_id="overview-observation",
    )
    return database, user


def test_full_overview_has_sourced_dimensions_and_evidence_assessments():
    database, user = _seed_full()

    result = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 3)
    )

    dimensions = {item["key"]: item for item in result["profile_dimensions"]}
    assert result["participant_code"] == "P002"
    assert result["current_state"]["stress_0_10"] == 6.4
    assert result["risk_summary"]["peak_stress"] == 8.0
    assert result["current_model"]["learned_profile_version"] == 4
    assert dimensions["workload_exposure"]["score_0_100"] == 70.0
    assert dimensions["stress_reactivity"]["source_field"] == "stress_reactivity_i"
    assert dimensions["recovery_capacity"]["score_0_100"] == 65.0
    assert dimensions["schedule_volatility"]["sample_count"] == 3
    assert all(item["source"] and item["normalization"] for item in dimensions.values())
    assert all(item["evidence_keys"] for item in result["system_assessment"])
    assert result["care_summary"]["analysis_type"] == "observational_descriptive"
    assert result["care_summary"]["causal_claim_allowed"] is False


def test_missing_data_stays_none_and_dimensions_are_not_filled_with_default_50():
    database = memory_database()
    user = participant(database, "P-EXPLICIT")
    with database.session() as session:
        session.get(Participant, user.id).created_at = datetime(
            2026, 8, 20, tzinfo=timezone.utc
        )
        session.add(
            ParticipantProfile(
                participant_id=user.id,
                version=1,
                profile_json={"schema_version": "2.0", "explicit": {"language": "zh"}},
            )
        )

    result = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 3)
    )

    assert result["current_state"]["stress_0_10"] is None
    assert result["risk_summary"]["peak_stress"] is None
    assert result["risk_summary"]["attention_level"] is None
    assert result["behavior_summary"]["daily_review_completion_rate_14d"] == 0.0
    keys = {item["key"] for item in result["profile_dimensions"]}
    assert "stress_reactivity" not in keys
    assert "workload_exposure" not in keys
    assert all(item["score_0_100"] != 50 for item in result["profile_dimensions"])


def test_overview_api_is_participant_bound_and_requires_authentication():
    database, _user = _seed_full()
    browser = TestClient(create_app(database, settings()))

    assert browser.get("/admin/api/participants/P002/overview").status_code == 401
    login(browser)
    response = browser.get("/admin/api/participants/P002/overview?through=2026-09-03")

    assert response.status_code == 200
    assert response.json()["schema_version"] == "participant-overview.v2"
    assert response.json()["provenance"]["clinical_diagnosis"] is False
