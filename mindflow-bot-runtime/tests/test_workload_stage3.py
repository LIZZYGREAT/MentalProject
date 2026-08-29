from datetime import date, datetime, timezone
import inspect

import pytest

from app.models import (
    EventAppraisalFeedback,
    ForecastCurrentnessEvent,
    ForecastSnapshot,
    StateObservation,
)
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
    workload_revision,
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
    assert fit.fit_scope == "exploratory_in_sample"
    assert "mae_in_sample" in fit.to_dict()
    assert "mae" not in fit.to_dict()
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


def test_event_appraisal_persists_observed_workload_and_residual(monkeypatch):
    database = memory_database()
    person = participant(database, "P001")
    repository = EventAppraisalFeedbackRepository(database)
    fixed_time = datetime(2030, 1, 15, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.repositories.utc_now", lambda: fixed_time)
    features = {
        "mental_demand": 0.8,
        "physical_demand": 0.2,
        "temporal_demand": 0.7,
        "effort": 0.8,
        "frustration": 0.6,
    }
    with database.session() as session:
        forecast = ForecastSnapshot(
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
                    "workload_schema_version": "event_workload.v1",
                    "workload_model_version": "workload-rules-logistic.v1",
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
            generated_at=datetime(2030, 1, 15, 0, tzinfo=timezone.utc),
        )
        session.add(forecast)
        session.flush()
        session.add(ForecastCurrentnessEvent(
            participant_id=person.id,
            local_date=date(2030, 1, 15),
            forecast_id=forecast.id,
            forecast_version=forecast.forecast_version,
            event_type="activated",
            reason="test",
            occurred_at=datetime(2030, 1, 15, 0, tzinfo=timezone.utc),
        ))
    item = repository.record(
        person.id,
        event_id="event-1",
        submitted_at=datetime(2030, 1, 15, 12, tzinfo=timezone.utc),
        event_local_date=date(2030, 1, 15),
        event_start_at=datetime(2030, 1, 15, 1, tzinfo=timezone.utc),
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
    assert item["source_forecast_version"] == "f-context"
    assert item["created_at"] == fixed_time.isoformat()


def test_admin_workload_diagnostics_reports_lags_bins_and_appraisal_calibration():
    database = memory_database()
    person = participant(database, "P001")
    with database.session() as session:
        forecast = ForecastSnapshot(
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
            generated_at=datetime(2030, 1, 15, 0, tzinfo=timezone.utc),
        )
        session.add(forecast)
        session.flush()
        session.add(ForecastCurrentnessEvent(
            participant_id=person.id,
            local_date=date(2030, 1, 15),
            forecast_id=forecast.id,
            forecast_version=forecast.forecast_version,
            event_type="activated",
            reason="test",
            occurred_at=datetime(2030, 1, 15, 0, tzinfo=timezone.utc),
        ))
        session.add(StateObservation(
            participant_id=person.id,
            observation_type="checkin",
            payload_json={"stress_0_10": 8.0},
            observed_at=datetime(2030, 1, 15, 1, 5, tzinfo=timezone.utc),
            created_at=datetime(2030, 1, 15, 1, 6, tzinfo=timezone.utc),
            source_message_id="m1",
        ))
    result = ResearchEvaluationService(database, "Asia/Shanghai").workload_diagnostics(
        date(2030, 1, 15), date(2030, 1, 15), person.id
    )
    assert result["actual_ema"][0]["workload"] == 0.8
    assert [item["lag_minutes"] for item in result["statistics"]["lagged_corr"]] == [0, 5, 10, 15, 30, 60]
    assert result["statistics"]["mae_by_workload_bin"][0]["mean_residual"] == 1.0
    assert result["series_mode"] == "latest_descriptive"


def test_event_appraisal_freezes_exact_causal_forecast_and_never_uses_later_revision(
    monkeypatch,
):
    database = memory_database()
    person = participant(database, "P001")
    fixed_time = datetime(2030, 1, 16, 11, tzinfo=timezone.utc)
    monkeypatch.setattr("app.repositories.utc_now", lambda: fixed_time)
    forecasts = []
    with database.session() as session:
        for version, prior, activated_at, valid in (
            ("forecast-a", 0.2, datetime(2030, 1, 15, 0, tzinfo=timezone.utc), False),
            ("forecast-b", 0.9, datetime(2030, 1, 15, 10, tzinfo=timezone.utc), True),
        ):
            row = ForecastSnapshot(
                participant_id=person.id,
                local_date=date(2030, 1, 15),
                calendar_revision="c",
                semantic_revision=f"semantic-{version}",
                observation_revision="o",
                algorithm_version="v",
                forecast_version=version,
                semantic_status="rules_only",
                semantic_input_json=[{
                    "event_id": "event-1",
                    "semantic": {
                        "workload_feature_vector": {
                            name: prior for name in (
                                "mental_demand", "physical_demand",
                                "temporal_demand", "effort", "frustration",
                            )
                        },
                        "workload_prior": prior,
                        "workload_schema_version": "event_workload.v1",
                        "workload_model_version": "workload-rules-logistic.v1",
                    },
                }],
                curve_json=[], peaks_json=[], warning_windows_json=[],
                output_json={"classified_calendar_events": [{
                    "id": "event-1", "event_type": version,
                    "course_name": version,
                }]},
                valid=valid,
                generated_at=activated_at,
            )
            session.add(row)
            session.flush()
            session.add(ForecastCurrentnessEvent(
                participant_id=person.id,
                local_date=date(2030, 1, 15),
                forecast_id=row.id,
                forecast_version=version,
                event_type="activated",
                reason="test",
                occurred_at=activated_at,
            ))
            forecasts.append(row.id)

    scores = dict(
        mental_demand=8, physical_demand=2, temporal_demand=7, effort=8,
        frustration=6, perceived_control=4, actual_stress=7,
        perceived_performance=6,
    )
    item = EventAppraisalFeedbackRepository(database).record(
        person.id,
        event_id="event-1",
        event_local_date=date(2030, 1, 15),
        event_start_at=datetime(2030, 1, 15, 9, tzinfo=timezone.utc),
        submitted_at=datetime(2030, 1, 15, 11, tzinfo=timezone.utc),
        **scores,
    )
    assert item["source_forecast_id"] == str(forecasts[0])
    assert item["source_forecast_version"] == "forecast-a"
    assert item["source_semantic_revision"] == "semantic-forecast-a"
    assert item["event_type"] == "forecast-a"
    assert item["workload_prior"] == 0.2
    assert item["workload_residual"] == pytest.approx(0.42)
    assert item["created_at"] == fixed_time.isoformat()

    no_causal = EventAppraisalFeedbackRepository(database).record(
        person.id,
        event_id="missing-event",
        event_local_date=date(2030, 1, 16),
        event_start_at=datetime(2030, 1, 16, 1, tzinfo=timezone.utc),
        submitted_at=datetime(2030, 1, 16, 11, tzinfo=timezone.utc),
        **scores,
    )
    assert no_causal["observed_workload"] == pytest.approx(0.62)
    assert no_causal["workload_prior"] is None
    assert no_causal["workload_residual"] is None
    assert no_causal["source_forecast_id"] is None


def test_event_appraisal_system_clock_controls_created_at_and_causal_cutoff(
    monkeypatch,
):
    database = memory_database()
    person = participant(database, "P001")
    fixed_time = datetime(2030, 1, 15, 9, tzinfo=timezone.utc)
    monkeypatch.setattr("app.repositories.utc_now", lambda: fixed_time)
    with database.session() as session:
        for version, prior, activated_at, valid in (
            ("forecast-a", 0.2, datetime(2030, 1, 15, 8, tzinfo=timezone.utc), False),
            ("forecast-b", 0.9, datetime(2030, 1, 15, 10, tzinfo=timezone.utc), True),
        ):
            row = ForecastSnapshot(
                participant_id=person.id,
                local_date=date(2030, 1, 15),
                calendar_revision="c",
                semantic_revision=f"s-{version}",
                observation_revision="o",
                algorithm_version="v",
                forecast_version=version,
                semantic_status="rules_only",
                semantic_input_json=[{
                    "event_id": "event-1",
                    "semantic": {
                        "workload_feature_vector": {
                            name: prior for name in (
                                "mental_demand", "physical_demand",
                                "temporal_demand", "effort", "frustration",
                            )
                        },
                        "workload_prior": prior,
                    },
                }],
                curve_json=[], peaks_json=[], warning_windows_json=[],
                output_json={"classified_calendar_events": [{
                    "id": "event-1", "event_type": "exam",
                }]},
                valid=valid, generated_at=activated_at,
            )
            session.add(row)
            session.flush()
            session.add(ForecastCurrentnessEvent(
                participant_id=person.id,
                local_date=date(2030, 1, 15),
                forecast_id=row.id,
                forecast_version=version,
                event_type="activated",
                reason="test",
                occurred_at=activated_at,
            ))
    scores = dict(
        mental_demand=8, physical_demand=2, temporal_demand=7, effort=8,
        frustration=6, perceived_control=4, actual_stress=7,
        perceived_performance=6,
    )
    repository = EventAppraisalFeedbackRepository(database)
    item = repository.record(
        person.id, event_id="event-1",
        event_local_date=date(2030, 1, 15),
        event_start_at=datetime(2030, 1, 15, 12, tzinfo=timezone.utc),
        submitted_at=datetime(2030, 1, 15, 13, tzinfo=timezone.utc),
        **scores,
    )
    assert item["created_at"] == fixed_time.isoformat()
    assert item["source_forecast_version"] == "forecast-a"
    assert item["workload_prior"] == 0.2
    assert "created_at" not in inspect.signature(repository.record).parameters
    with pytest.raises(ValueError, match="created_at"):
        repository.record(
            person.id, event_id="event-1",
            event_local_date=date(2030, 1, 15),
            event_start_at=datetime(2030, 1, 15, 12, tzinfo=timezone.utc),
            submitted_at=datetime(2030, 1, 15, 13, tzinfo=timezone.utc),
            created_at=datetime(2029, 1, 1, tzinfo=timezone.utc),
            **scores,
        )


@pytest.mark.parametrize(
    "feature_vector",
    [
        {
            "mental_demand": 0.8, "physical_demand": 0.2,
            "temporal_demand": 0.7, "effort": 0.8,
        },
        {
            "mental_demand": "invalid", "physical_demand": 0.2,
            "temporal_demand": 0.7, "effort": 0.8, "frustration": 0.6,
        },
        {
            "mental_demand": 0.8, "physical_demand": 1.2,
            "temporal_demand": 0.7, "effort": 0.8, "frustration": 0.6,
        },
    ],
)
def test_event_appraisal_malformed_workload_context_fails_soft(
    monkeypatch, feature_vector,
):
    database = memory_database()
    person = participant(database, "P001")
    fixed_time = datetime(2030, 1, 15, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.repositories.utc_now", lambda: fixed_time)
    with database.session() as session:
        forecast = ForecastSnapshot(
            participant_id=person.id,
            local_date=date(2030, 1, 15),
            calendar_revision="c", semantic_revision="s-malformed",
            observation_revision="o", algorithm_version="v",
            forecast_version="f-malformed", semantic_status="rules_only",
            semantic_input_json=[{
                "event_id": "event-1",
                "semantic": {
                    "workload_feature_vector": feature_vector,
                    "workload_prior": 0.7,
                    "workload_schema_version": "event_workload.v1",
                    "workload_model_version": "workload-rules-logistic.v1",
                },
            }],
            curve_json=[], peaks_json=[], warning_windows_json=[],
            output_json={"classified_calendar_events": [{
                "id": "event-1", "event_type": "exam",
            }]},
            valid=True,
            generated_at=datetime(2030, 1, 15, 8, tzinfo=timezone.utc),
        )
        session.add(forecast)
        session.flush()
        session.add(ForecastCurrentnessEvent(
            participant_id=person.id,
            local_date=date(2030, 1, 15),
            forecast_id=forecast.id,
            forecast_version=forecast.forecast_version,
            event_type="activated", reason="test",
            occurred_at=datetime(2030, 1, 15, 8, tzinfo=timezone.utc),
        ))

    item = EventAppraisalFeedbackRepository(database).record(
        person.id, event_id="event-1",
        event_local_date=date(2030, 1, 15),
        event_start_at=datetime(2030, 1, 15, 9, tzinfo=timezone.utc),
        submitted_at=datetime(2030, 1, 15, 11, tzinfo=timezone.utc),
        mental_demand=8, physical_demand=2, temporal_demand=7, effort=8,
        frustration=6, perceived_control=4, actual_stress=7,
        perceived_performance=6,
    )
    assert item["mental_demand"] == 8
    assert item["observed_workload"] == pytest.approx(0.62)
    assert item["workload_feature_vector"] is None
    assert item["workload_prior"] is None
    assert item["workload_residual"] is None
    assert item["source_forecast_id"] is None
    assert item["source_forecast_version"] is None
    assert item["source_semantic_revision"] is None
    assert item["workload_schema_version"] is None
    assert item["workload_model_version"] is None


def test_ema_workload_statistics_use_historical_currentness_not_latest_valid():
    database = memory_database()
    person = participant(database, "P001")
    with database.session() as session:
        for version, workload, activated_at, valid in (
            ("forecast-a", 0.25, datetime(2030, 1, 15, 0, tzinfo=timezone.utc), False),
            ("forecast-b", 0.95, datetime(2030, 1, 15, 2, tzinfo=timezone.utc), True),
        ):
            row = ForecastSnapshot(
                participant_id=person.id, local_date=date(2030, 1, 15),
                calendar_revision="c", semantic_revision=f"s-{version}",
                observation_revision="o", algorithm_version="v",
                forecast_version=version, semantic_status="rules_only",
                semantic_input_json=[],
                curve_json=[
                    {"time": "09:00", "workload": workload, "stress_0_10": 4.0},
                    {"time": "09:05", "workload": workload, "stress_0_10": 5.0},
                ],
                peaks_json=[], warning_windows_json=[],
                output_json={"classified_calendar_events": []},
                valid=valid, generated_at=activated_at,
            )
            session.add(row)
            session.flush()
            session.add(ForecastCurrentnessEvent(
                participant_id=person.id, local_date=date(2030, 1, 15),
                forecast_id=row.id, forecast_version=version,
                event_type="activated", reason="test", occurred_at=activated_at,
            ))
        session.add(StateObservation(
            participant_id=person.id, observation_type="checkin",
            payload_json={"stress_0_10": 8.0}, source_message_id="causal-ema",
            observed_at=datetime(2030, 1, 15, 1, 5, tzinfo=timezone.utc),
            created_at=datetime(2030, 1, 15, 1, 6, tzinfo=timezone.utc),
        ))

    result = ResearchEvaluationService(database, "Asia/Shanghai").workload_diagnostics(
        date(2030, 1, 15), date(2030, 1, 15), person.id
    )
    assert result["series"][0]["workload"] == 0.95
    assert result["actual_ema"][0]["workload"] == 0.25
    assert result["actual_ema"][0]["source_forecast_version"] == "forecast-a"
    assert result["statistics"]["lagged_corr"][0]["sample_count"] == 1
    assert result["statistics"]["lagged_corr"][1]["sample_count"] == 1


def test_stage3_physical_metadata_changes_workload_but_not_ctssm_or_alerts():
    values = {
        "difficulty": 0.7, "cognitive_demand": 0.8, "stakes": 0.6,
        "time_pressure": 0.7, "social_evaluation": 0.4,
        "uncontrollability": 0.5, "novelty": 0.3,
        "expected_effort": 0.8, "uncertainty": 0.4, "unfinished": 0.2,
    }
    base = {
        "id": "event-1", "summary": "考试", "event_type": "task",
        "task_type": "exam", "start_time": "2030-01-15T09:00:00+08:00",
        "end_time": "2030-01-15T11:00:00+08:00",
        "metadata": {"semantic": {
            "values": values,
            "fused": {"appraisal_score_1_10": 4.0},
        }},
    }
    enriched = {
        **base,
        "metadata": {"semantic": {
            "values": {**values, "physical_demand": 0.95},
            "fused": {"appraisal_score_1_10": 4.0},
            "workload_feature_vector": {
                "mental_demand": 0.8, "physical_demand": 0.95,
                "temporal_demand": 0.7, "effort": 0.8, "frustration": 0.5,
            },
            "workload_prior": 0.9,
        }},
    }
    model = AssessmentModel("Asia/Shanghai")
    stage2 = model.predict(profile={}, observations=[], calendar_events=[base], local_date="2030-01-15")
    stage3 = model.predict(profile={}, observations=[], calendar_events=[enriched], local_date="2030-01-15")
    assert [(p["stress_0_10"], p["vitality_0_10"]) for p in stage2.trajectory] == [
        (p["stress_0_10"], p["vitality_0_10"]) for p in stage3.trajectory
    ]
    assert stage2.alerts == stage3.alerts
    assert [p["workload"] for p in stage2.trajectory] != [
        p["workload"] for p in stage3.trajectory
    ]


def test_workload_revision_covers_continuous_load_configuration():
    baseline = workload_revision({})
    assert baseline == workload_revision({})
    assert baseline != workload_revision({
        "model_params": {"ctssm_params": {"workload_continuous_beta": 0.19}}
    })


def test_appraisal_ridge_is_exploratory_and_residuals_are_version_separated():
    database = memory_database()
    person = participant(database, "P001")
    feature_vector = {
        "mental_demand": 0.8, "physical_demand": 0.2,
        "temporal_demand": 0.7, "effort": 0.8, "frustration": 0.6,
    }
    with database.session() as session:
        for index, (version, residual) in enumerate((
            ("workload-v1", 0.1), ("workload-v2", -0.2)
        )):
            session.add(EventAppraisalFeedback(
                participant_id=person.id, event_id=f"event-{index}",
                mental_demand=8, physical_demand=2, temporal_demand=7,
                effort=8, frustration=6, perceived_control=4,
                actual_stress=7, perceived_performance=6,
                event_type="exam", workload_feature_vector=feature_vector,
                workload_prior=0.5, observed_workload=0.5 + residual,
                workload_residual=residual, workload_model_version=version,
                submitted_at=datetime(2030, 1, 15, 12 + index, tzinfo=timezone.utc),
                created_at=datetime(2030, 1, 15, 12 + index, tzinfo=timezone.utc),
            ))

    appraisal = ResearchEvaluationService(
        database, "Asia/Shanghai"
    ).workload_diagnostics(
        date(2030, 1, 15), date(2030, 1, 15), person.id
    )["event_appraisal"]
    assert appraisal["ridge_fit"]["status"] == (
        "separated_by_workload_model_version"
    )
    assert all(
        item["status"] == "insufficient_sample"
        and "coefficients" not in item
        for item in appraisal["ridge_fit_by_model_version"]
    )
    residuals = appraisal["residual_by"]["event_type"]
    assert {item["workload_model_version"] for item in residuals} == {
        "workload-v1", "workload-v2"
    }
