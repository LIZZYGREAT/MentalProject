from datetime import date, datetime, time, timezone
import uuid

from starlette.testclient import TestClient

from app.admin_web.main import create_app
from app.models import (
    DatasetSnapshot,
    ForecastSnapshot,
    LearnedModelProfile,
    ModelEvaluationRun,
    ModelPromotionDecision,
    Participant,
    ParticipantProfile,
    ParticipantSlowState,
    StateObservation,
)
from app.repositories import (
    ForecastSnapshotRepository,
    ObservationRepository,
    promotion_parameters_hash,
)
from app.services.participant_overview import ParticipantOverviewService
from helpers import memory_database, participant
from test_admin_web import login, settings


def _seed_full():
    database = memory_database()
    user = participant(database, "P002")
    seeded_at = datetime(2026, 9, 2, 0, tzinfo=timezone.utc)
    with database.session() as session:
        session.get(Participant, user.id).created_at = datetime(
            2026, 8, 20, tzinfo=timezone.utc
        )
        session.add(
            ParticipantProfile(
                participant_id=user.id,
                version=2,
                profile_json={"schema_version": "2.0", "explicit": {"support": "brief"}},
                created_at=seeded_at,
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
                created_at=seeded_at,
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
                created_at=seeded_at,
            )
        )
    repo = ForecastSnapshotRepository(database)
    for offset, workload in enumerate((0.2, 0.5, 0.8)):
        day = date(2026, 9, 1 + offset)
        curve = [
            {"time": "08:00", "stress_0_10": 4 + offset, "workload": workload},
            {"time": "16:00", "stress_0_10": 7 + offset / 2, "workload": workload},
        ]
        saved = repo.save(
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
        with database.session() as session:
            session.get(ForecastSnapshot, uuid.UUID(saved["id"])).generated_at = datetime.combine(
                day, time(1), tzinfo=timezone.utc
            )
    observation_id = ObservationRepository(database).add(
        user.id,
        "instant_checkin",
        {"stress_0_10": 6.4, "energy_0_10": 4.2},
        observed_at=datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
        source_message_id="overview-observation",
    )
    with database.session() as session:
        session.get(StateObservation, observation_id).created_at = datetime(
            2026, 9, 3, 8, tzinfo=timezone.utc
        )
    return database, user


def _save_forecast(
    database,
    participant_id,
    local_date,
    *,
    stress=7.0,
    workload=0.5,
    version="test-forecast",
):
    saved = ForecastSnapshotRepository(database).save(
        participant_id,
        local_date,
        calendar_revision=f"calendar-{version}",
        semantic_revision=f"semantic-{version}",
        observation_revision=f"observation-{version}",
        algorithm_version="forecast.v4",
        forecast_version=version,
        semantic_status="complete",
        semantic_input=[],
        curve=[{"time": "15:00", "stress_0_10": stress, "workload": workload}],
        peaks=[],
        warning_windows=[],
        output={"model_family": "stress-ctssm.m3"},
    )
    with database.session() as session:
        session.get(ForecastSnapshot, uuid.UUID(saved["id"])).generated_at = datetime.combine(
            min(local_date, date(2026, 9, 4)), time(1), tzinfo=timezone.utc
        )


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
    assert dimensions["workload_volatility"]["label"] == "任务负荷波动"
    assert dimensions["workload_volatility"]["source_field"] == "daily_mean(workload)"
    assert dimensions["workload_volatility"]["sample_count"] == 3
    assert "每日平均 W(t) 的标准差" in dimensions["workload_volatility"]["description"]
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
    assert response.json()["schema_version"] == "participant-overview.v3"
    assert response.json()["provenance"]["clinical_diagnosis"] is False


def test_missing_target_day_forecast_does_not_relabel_yesterday_peak_as_today():
    database = memory_database()
    user = participant(database, "P-YESTERDAY")
    _save_forecast(database, user.id, date(2026, 9, 3), stress=8.4)

    result = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 4)
    )

    assert result["risk_summary"]["peak_stress"] is None
    assert result["risk_summary"]["attention_level"] is None
    assert result["current_model"]["forecast_date"] == "2026-09-03"


def test_future_forecast_is_excluded_from_latest_model_and_target_day_risk():
    database = memory_database()
    user = participant(database, "P-FUTURE")
    _save_forecast(database, user.id, date(2026, 9, 3), version="past")
    _save_forecast(database, user.id, date(2026, 9, 5), stress=9.7, version="future")

    result = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 4)
    )

    assert result["current_model"]["forecast_date"] == "2026-09-03"
    assert result["risk_summary"]["peak_stress"] is None


def test_historical_through_excludes_future_observation():
    database = memory_database()
    user = participant(database, "P-OBSERVATION")
    repository = ObservationRepository(database)
    older_id = repository.add(
        user.id,
        "instant_checkin",
        {"stress_0_10": 4.2},
        observed_at=datetime(2026, 9, 3, 2, tzinfo=timezone.utc),
        source_message_id="historical-observation",
    )
    future_id = repository.add(
        user.id,
        "instant_checkin",
        {"stress_0_10": 9.1},
        observed_at=datetime(2026, 9, 4, 2, tzinfo=timezone.utc),
        source_message_id="future-observation",
    )
    with database.session() as session:
        session.get(StateObservation, older_id).created_at = datetime(
            2026, 9, 3, 2, tzinfo=timezone.utc
        )
        session.get(StateObservation, future_id).created_at = datetime(
            2026, 9, 4, 2, tzinfo=timezone.utc
        )

    result = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 3)
    )

    assert result["current_state"]["stress_0_10"] == 4.2
    assert result["current_state"]["latest_observed_at"].startswith("2026-09-03")


def _learned_profile(
    participant_id,
    version,
    status,
    reactivity,
    created_at,
    *,
    model_selection=None,
):
    parameters = {"stress_reactivity_i": reactivity}
    if model_selection is not None:
        parameters["model_selection"] = model_selection
    return LearnedModelProfile(
        participant_id=participant_id,
        version=version,
        parameters_json=parameters,
        uncertainty_json={"stress_reactivity_i": {"standard_error": 0.1}},
        source="overview-test",
        model_version="overview-test.v1",
        validation_status=status,
        sample_count=30 + version,
        day_count=14,
        confidence=0.8,
        window_start=date(2026, 8, 15),
        window_end=date(2026, 8, 31),
        created_at=created_at,
    )


def test_latest_candidate_does_not_replace_older_runtime_active_profile():
    database = memory_database()
    user = participant(database, "P-LATEST-CANDIDATE")
    with database.session() as session:
        session.add_all(
            [
                _learned_profile(
                    user.id,
                    1,
                    "validated",
                    0.45,
                    datetime(2026, 8, 30, tzinfo=timezone.utc),
                ),
                _learned_profile(
                    user.id,
                    2,
                    "candidate",
                    1.35,
                    datetime(2026, 9, 1, tzinfo=timezone.utc),
                ),
            ]
        )

    result = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 2)
    )

    assert result["current_model"]["learned_profile_version"] == 1
    assert result["latest_learned_profile"]["version"] == 2
    assert result["latest_learned_profile"]["is_runtime_active"] is False
    assert result["key_parameters"][0]["estimate"] == 0.45


def test_latest_rejected_does_not_replace_older_runtime_active_profile():
    database = memory_database()
    user = participant(database, "P-LATEST-REJECTED")
    with database.session() as session:
        session.add_all(
            [
                _learned_profile(
                    user.id,
                    3,
                    "validated",
                    0.55,
                    datetime(2026, 8, 30, tzinfo=timezone.utc),
                ),
                _learned_profile(
                    user.id,
                    4,
                    "rejected",
                    1.45,
                    datetime(2026, 9, 1, tzinfo=timezone.utc),
                ),
            ]
        )

    result = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 2)
    )

    assert result["current_model"]["learned_profile_version"] == 3
    assert result["latest_learned_profile"]["version"] == 4
    assert result["latest_learned_profile"]["validation_status"] == "rejected"
    assert result["latest_learned_profile"]["is_runtime_active"] is False
    assert result["key_parameters"][0]["estimate"] == 0.55


def test_candidate_only_profile_is_not_used_as_runtime_active_fallback():
    database = memory_database()
    user = participant(database, "P-CANDIDATE-ONLY")
    with database.session() as session:
        session.add(
            _learned_profile(
                user.id,
                1,
                "candidate",
                1.2,
                datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
        )

    result = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 2)
    )

    assert result["current_model"]["learned_profile_version"] is None
    assert result["current_model"]["validation_status"] is None
    assert result["latest_learned_profile"]["version"] == 1
    assert result["latest_learned_profile"]["is_runtime_active"] is False
    assert result["key_parameters"] == []
    assert "stress_reactivity" not in {
        item["key"] for item in result["profile_dimensions"]
    }


def _stage4_promoted_profile(
    session,
    participant_id,
    *,
    version,
    profile_created_at,
    proof_created_at,
    reactivity,
):
    snapshot_id = uuid.uuid4()
    evaluation_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    snapshot = DatasetSnapshot(
        id=snapshot_id,
        created_at=proof_created_at,
        date_start=date(2026, 8, 1),
        date_end=date(2026, 8, 31),
        participant_filter={"participant_id": str(participant_id)},
        observation_cutoff=proof_created_at,
        calendar_cutoff=proof_created_at,
        schema_version="mindflow-research-dataset-v7",
        manifest_json={},
    )
    evaluation = ModelEvaluationRun(
        id=evaluation_id,
        dataset_snapshot_id=snapshot_id,
        model_version="overview-stage4.v1",
        evaluation_mode="offline_replay",
        evaluation_code_version="overview-test.v1",
        participant_id=participant_id,
        metrics_json={},
        created_at=proof_created_at,
        status="completed",
    )
    parameters = {"stress_reactivity_i": reactivity}
    decision = ModelPromotionDecision(
        id=decision_id,
        model_evaluation_run_id=evaluation_id,
        dataset_snapshot_id=snapshot_id,
        participant_id=participant_id,
        model_family="m1",
        promotion_gate_version="overview-test-gate.v1",
        evaluation_code_version="overview-test.v1",
        parameters_hash=promotion_parameters_hash(parameters),
        status="retained_from_empirical_evidence",
        passed_at=proof_created_at,
        promoted_at=proof_created_at,
    )
    profile = _learned_profile(
        participant_id,
        version,
        "validated",
        reactivity,
        profile_created_at,
        model_selection={
            "active_variant": "m1",
            "promotion_decision_id": str(decision_id),
        },
    )
    session.add_all([snapshot, evaluation, decision, profile])


def test_historical_cutoff_keeps_profile_active_before_later_promotion():
    database = memory_database()
    user = participant(database, "P-HISTORICAL-ACTIVE")
    with database.session() as session:
        session.add(
            _learned_profile(
                user.id,
                3,
                "validated",
                0.5,
                datetime(2026, 8, 30, tzinfo=timezone.utc),
            )
        )
        _stage4_promoted_profile(
            session,
            user.id,
            version=4,
            profile_created_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
            proof_created_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
            reactivity=1.1,
        )

    historical = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 1)
    )
    current = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 4)
    )

    assert historical["current_model"]["learned_profile_version"] == 3
    assert historical["latest_learned_profile"]["version"] == 3
    assert current["current_model"]["learned_profile_version"] == 4


def test_profile_before_cutoff_with_promotion_proof_after_cutoff_fails_closed():
    database = memory_database()
    user = participant(database, "P-LATE-PROOF")
    with database.session() as session:
        _stage4_promoted_profile(
            session,
            user.id,
            version=1,
            profile_created_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
            proof_created_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            reactivity=1.0,
        )

    historical = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 1)
    )
    current = ParticipantOverviewService(database, "Asia/Shanghai").build(
        user.id, through=date(2026, 9, 2)
    )

    assert historical["latest_learned_profile"]["version"] == 1
    assert historical["current_model"]["learned_profile_version"] is None
    assert historical["key_parameters"] == []
    assert current["current_model"]["learned_profile_version"] == 1
