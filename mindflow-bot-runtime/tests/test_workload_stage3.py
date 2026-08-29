from datetime import date, datetime, timezone

import pytest

from app.models import ForecastSnapshot, StateObservation
from app.repositories import EventAppraisalFeedbackRepository
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.research_evaluation import ResearchEvaluationService
from helpers import memory_database, participant
from mindflow_core.assessment import AssessmentModel
from services.workload import (
    WorkloadEstimator,
    apply_continuous_load,
    event_workload_contribution,
    observed_workload,
    saturating_union,
    workload_feature_vector,
)


def test_workload_feature_mapping_prior_observation_and_ridge_are_bounded():
    features = workload_feature_vector(
        {
            "difficulty": 0.8,
            "cognitive_demand": 0.9,
            "physical_demand": 0.2,
            "time_pressure": 0.7,
            "expected_effort": 0.85,
            "uncontrollability": 0.6,
            "uncertainty": 0.4,
        }
    )
    estimate = WorkloadEstimator().estimate(features)
    assert features["mental_demand"] == pytest.approx(0.865)
    assert 0.0 < estimate.workload_prior < 1.0
    observed = observed_workload(
        {
            "mental_demand": 8,
            "physical_demand": 2,
            "temporal_demand": 7,
            "effort": 8,
            "frustration": 6,
            "perceived_control": 4,
        }
    )
    assert observed == pytest.approx(0.62)
    fitted, fit = WorkloadEstimator.fit_ridge(
        [features, {name: 0.1 for name in features}], [observed, 0.15], alpha=0.5
    )
    assert fit.sample_count == 2
    assert 0.0 <= fitted.predict(features) <= 1.0


def test_time_varying_concurrency_and_continuous_load_follow_stage3_equations():
    active, phase = event_workload_contribution(
        0.7, minutes_before_start=0, minutes_after_end=-1, active=True
    )
    pre, pre_phase = event_workload_contribution(
        0.7, minutes_before_start=90, minutes_after_end=-1, active=False
    )
    post, post_phase = event_workload_contribution(
        0.7, minutes_before_start=-1, minutes_after_end=120, active=False
    )
    assert (phase, pre_phase, post_phase) == ("active", "anticipation", "aftermath")
    assert active == pytest.approx(0.7)
    assert pre == pytest.approx(0.7 / 2.718281828459045)
    assert post == pytest.approx(0.7 / 2.718281828459045)
    assert saturating_union([0.7, 0.6]) == pytest.approx(0.88)
    adjusted, factor = apply_continuous_load(0.6, 1.5, saturation_hours=3, beta=0.2)
    assert factor == pytest.approx(0.5)
    assert adjusted == pytest.approx(0.7)


def test_semantic_enrichment_and_forecast_expose_workload_without_new_state():
    database = memory_database()
    person = participant(database, "P001")
    preprocessor = EventSemanticPreprocessor(
        cache=__import__("app.repositories", fromlist=["EventSemanticCacheRepository"])
        .EventSemanticCacheRepository(database),
        client=None,
        model="none",
    )
    raw = [{
        "id": "exam-1",
        "summary": "期末考试",
        "description": "限时考试",
        "event_type": "task",
        "task_type": "exam",
        "start_time": "2030-01-15T09:00:00+08:00",
        "end_time": "2030-01-15T12:00:00+08:00",
    }]
    prepared, *_ = preprocessor.prepare(person.id, raw, consent=False)
    semantic = prepared[0]["metadata"]["semantic"]
    assert "physical_demand" in semantic["values"]
    assert set(semantic["workload_feature_vector"]) == {
        "mental_demand", "physical_demand", "temporal_demand", "effort", "frustration"
    }
    result = AssessmentModel("Asia/Shanghai").predict(
        profile={}, observations=[], calendar_events=prepared, local_date="2030-01-15"
    )
    active = next(point for point in result.trajectory if point["time"] == "10:00")
    before = next(point for point in result.trajectory if point["time"] == "08:00")
    assert 0.0 < before["workload"] < active["workload"] <= 1.0
    assert "W" not in result.active_states


def test_event_appraisal_persists_observed_workload_and_residual():
    database = memory_database()
    person = participant(database, "P001")
    repository = EventAppraisalFeedbackRepository(database)
    features = {
        "mental_demand": 0.8,
        "physical_demand": 0.2,
        "temporal_demand": 0.7,
        "effort": 0.8,
        "frustration": 0.6,
    }
    with database.session() as session:
        session.add(ForecastSnapshot(
            participant_id=person.id,
            local_date=date(2030, 1, 15),
            calendar_revision="c-context",
            semantic_revision="s-context",
            observation_revision="o-context",
            algorithm_version="v",
            forecast_version="f-context",
            semantic_status="rules_only",
            semantic_input_json=[{
                "event_id": "event-1",
                "semantic": {
                    "workload_feature_vector": features,
                    "workload_prior": 0.7,
                },
            }],
            curve_json=[],
            peaks_json=[],
            warning_windows_json=[],
            output_json={
                "classified_calendar_events": [{
                    "id": "event-1",
                    "event_type": "exam",
                    "related_course_name": "高等数学",
                    "workload_prior": 0.7,
                }]
            },
            valid=True,
        ))
    item = repository.record(
        person.id,
        event_id="event-1",
        submitted_at=datetime(2030, 1, 15, 12, tzinfo=timezone.utc),
        mental_demand=8,
        physical_demand=2,
        temporal_demand=7,
        effort=8,
        frustration=6,
        perceived_control=4,
        actual_stress=7,
        perceived_performance=6,
    )
    assert item["observed_workload"] == pytest.approx(0.62)
    assert item["workload_residual"] == pytest.approx(-0.08)
    assert item["event_type"] == "exam"


def test_admin_workload_diagnostics_reports_lags_bins_and_appraisal_calibration():
    database = memory_database()
    person = participant(database, "P001")
    with database.session() as session:
        session.add(ForecastSnapshot(
            participant_id=person.id,
            local_date=date(2030, 1, 15),
            calendar_revision="c",
            semantic_revision="s",
            observation_revision="o",
            algorithm_version="v",
            forecast_version="f",
            semantic_status="rules_only",
            semantic_input_json=[],
            curve_json=[
                {"time": "09:00", "workload": 0.4, "workload_raw": 0.4, "stress_0_10": 5.0},
                {"time": "09:05", "workload": 0.8, "workload_raw": 0.8, "stress_0_10": 7.0},
            ],
            peaks_json=[],
            warning_windows_json=[],
            output_json={"classified_calendar_events": []},
            valid=True,
        ))
        session.add(StateObservation(
            participant_id=person.id,
            observation_type="checkin",
            payload_json={"stress_0_10": 8.0},
            observed_at=datetime(2030, 1, 15, 1, 5, tzinfo=timezone.utc),
            source_message_id="m1",
        ))
    result = ResearchEvaluationService(database, "Asia/Shanghai").workload_diagnostics(
        date(2030, 1, 15), date(2030, 1, 15), person.id
    )
    assert result["actual_ema"][0]["workload"] == 0.8
    assert [item["lag_minutes"] for item in result["statistics"]["lagged_corr"]] == [0, 5, 10, 15, 30, 60]
    assert result["statistics"]["mae_by_workload_bin"][0]["mean_residual"] == 1.0
