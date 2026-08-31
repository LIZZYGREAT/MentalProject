from datetime import date, datetime, timezone
import uuid

from app.repositories import (
    ParticipantSlowStateRepository,
    PsychometricAssessmentRepository,
)
from app.services.model_comparison import (
    MODEL_FAMILIES,
    comparison_metrics,
    estimate_reactivity_and_recovery,
    estimate_response_rates,
    observed_recovery_efficiency,
    promotion_gate,
    rolling_origin_splits,
    trait_resilience_prior,
)
from app.services.research_evaluation import (
    DATASET_SCHEMA_V4,
    ResearchEvaluationService,
)
from mindflow_core.assessment import AssessmentModel
from tests.helpers import memory_database, participant


def _profile(variant: str) -> dict:
    return {
        "model_params": {
            "model_selection": {
                "active_variant": variant,
                "status": "research_candidate_run",
            }
        }
    }


def _calendar() -> list[dict]:
    return [
        {
            "id": "work",
            "summary": "高负荷任务",
            "event_type": "task",
            "task_type": "exam",
            "start_time": "2030-01-15T09:00:00+08:00",
            "end_time": "2030-01-15T11:00:00+08:00",
            "metadata": {},
        },
        {
            "id": "protected",
            "summary": "保护性休息",
            "event_type": "rest",
            "start_time": "2030-01-15T11:00:00+08:00",
            "end_time": "2030-01-15T11:30:00+08:00",
            "metadata": {"protected_break": True},
        },
    ]


def test_current_m0_is_stable_comparator_and_candidates_share_interface():
    model = AssessmentModel("Asia/Shanghai")
    results = {
        variant: model.predict(
            profile=_profile(variant),
            observations=[],
            calendar_events=_calendar(),
            local_date="2030-01-15",
        )
        for variant in ("m0", "wm0", "m1", "m2", "m3")
    }

    assert results["m0"].model_family == "stress-ctssm.m0"
    assert "recovery_resource" not in results["m0"].trajectory[0]
    assert results["wm0"].model_family == "workload-aware-stress-ctssm.m0"
    assert results["m1"].active_states == ("S", "V")
    assert results["m2"].active_states == ("S", "V", "P")
    assert results["m3"].active_states == ("S", "V", "P", "F")
    assert {
        result.model_variant for result in results.values()
    } == {"m0", "wm0", "m1", "m2", "m3"}

    workload_point = next(
        point for point in results["wm0"].trajectory if point["time"] == "10:00"
    )
    break_point = next(
        point for point in results["wm0"].trajectory if point["time"] == "11:10"
    )
    assert workload_point["workload"] > 0
    assert break_point["recovery_components"]["protected_break"] > 0
    assert break_point["recovery_resource"] > 0


def test_resilience_prior_parameter_estimation_and_observed_recovery_are_auditable():
    assert trait_resilience_prior({"mean": 1.0}) == 0.0
    assert trait_resilience_prior({"mean": 5.0}) == 1.0
    assert trait_resilience_prior({"total_mean": 3.5}) == 0.625
    assert trait_resilience_prior({"total": 24.0}) == 0.75
    assert trait_resilience_prior({"mean": 8.0}) is None
    samples = [
        {
            "participant_id": "p",
            "local_date": f"2030-01-0{day}",
            "observed_at": f"2030-01-0{day}T09:00:00+00:00",
            "actual_stress": 4.0 + workload * 3.0 - recovery * 2.0,
            "workload": workload,
            "recovery": recovery,
        }
        for day, workload, recovery in (
            (1, 0.1, 0.8),
            (2, 0.3, 0.6),
            (3, 0.7, 0.2),
            (4, 0.9, 0.1),
        )
    ]
    estimate = estimate_reactivity_and_recovery(
        samples, trait_resilience=0.75
    )
    assert estimate["sample_count"] == 4
    assert estimate["workload_reactivity_beta"] > 0
    assert estimate["recovery_beta"] > 0
    assert estimate["trait_resilience_prior"] == 0.75

    episodes = [
        {
            "participant_id": "p",
            "observed_at": "2030-01-01T09:00:00+00:00",
            "actual_stress": 8.0,
            "workload": 0.8,
            "recovery": 0.0,
        },
        {
            "participant_id": "p",
            "observed_at": "2030-01-01T10:00:00+00:00",
            "actual_stress": 6.0,
            "workload": 0.2,
            "recovery": 0.8,
        },
    ]
    recovery = observed_recovery_efficiency(episodes)
    assert recovery["episode_transition_count"] == 1
    assert recovery["observed_recovery_efficiency_per_hour"] == 2.0

    rate_samples = [
        {
            "participant_id": "p",
            "observed_at": "2030-01-01T09:00:00+00:00",
            "actual_stress": 4.0,
            "workload": 1.0,
            "recovery": 0.0,
        },
        {
            "participant_id": "p",
            "observed_at": "2030-01-01T10:00:00+00:00",
            "actual_stress": 6.0,
            "workload": 1.0,
            "recovery": 0.0,
        },
        {
            "participant_id": "p",
            "observed_at": "2030-01-01T11:00:00+00:00",
            "actual_stress": 4.0,
            "workload": 0.0,
            "recovery": 1.0,
        },
    ]
    rates = estimate_response_rates(
        rate_samples,
        {
            "stress_baseline_0_10": 4.0,
            "workload_reactivity_beta": 4.0,
            "recovery_beta": 2.0,
        },
    )
    assert rates["stress_reactivity_per_hour"] == 0.6931
    assert rates["stress_recovery_per_hour"] == 0.6931
    assert rates["response_transition_count"] == 1
    assert rates["recovery_transition_count"] == 1


def test_rolling_origin_metrics_and_promotion_gate_cover_all_stage4_checks():
    samples = [
        {"local_date": f"2030-01-0{day}"}
        for day in range(1, 6)
    ]
    splits = rolling_origin_splits(samples, minimum_training_days=2)
    assert len(splits) == 3
    assert splits[0]["training_days"] == ["2030-01-01", "2030-01-02"]
    assert splits[0]["test_days"] == ["2030-01-03"]
    assert set(MODEL_FAMILIES) == {
        "current_m0", "workload_aware_m0", "m1", "m2", "m3"
    }

    predictions = [
        {
            "participant_id": "p",
            "local_date": "2030-01-03",
            "observed_at": "2030-01-03T09:00:00+00:00",
            "actual_stress": 8.0,
            "predicted_stress": 7.5,
            "prediction_lower": 6.0,
            "prediction_upper": 9.0,
        },
        {
            "participant_id": "p",
            "local_date": "2030-01-03",
            "observed_at": "2030-01-03T10:00:00+00:00",
            "actual_stress": 5.0,
            "predicted_stress": 5.0,
            "prediction_lower": 4.0,
            "prediction_upper": 6.0,
        },
    ]
    metrics = comparison_metrics(predictions)
    for name in (
        "mae", "rmse", "median_absolute_error", "peak_magnitude_error",
        "peak_timing_error_minutes", "interval_90_coverage",
        "mean_interval_width", "high_stress_precision",
        "high_stress_recall", "pr_auc", "sample_count",
    ):
        assert name in metrics
    baseline = {
        **metrics,
        "mae": 1.0,
        "interval_90_coverage": 0.8,
        "peak_timing_error_minutes": 10.0,
        "high_stress_recall": 0.7,
    }
    candidate = {
        **metrics,
        "mae": 0.96,
        "interval_90_coverage": 0.8,
        "peak_timing_error_minutes": 9.0,
        "high_stress_recall": 0.7,
        "participant_effect": [{"participant_id": "p", "mae_delta": -0.04}],
    }
    gate = promotion_gate(baseline, candidate)
    assert gate["passed"] is True
    assert all(gate["checks"].values())
    assert gate["participant_effect"] == candidate["participant_effect"]


def test_dataset_v4_freezes_brs_and_slow_recovery_evidence():
    database = memory_database()
    person = participant(database, "STAGE4-SNAPSHOT")
    PsychometricAssessmentRepository(database).record(
        person.id,
        instrument_name="BRS",
        instrument_version="1",
        language="zh-CN",
        raw_items={"1": 4},
        scores={"mean": 4.0},
        administered_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    ParticipantSlowStateRepository(database).record(
        person.id,
        effective_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
        cadence="daily",
        source="test",
        values={"recent_recovery_quality": 7.0, "recent_sleep_debt": 1.0},
    )
    snapshot = ResearchEvaluationService(
        database, "Asia/Shanghai"
    ).create_dataset_snapshot(
        date_start=date(2030, 1, 1),
        date_end=date(2030, 1, 3),
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2030, 1, 4, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2030, 1, 4, tzinfo=timezone.utc),
    )
    assert snapshot["schema_version"] == DATASET_SCHEMA_V4
    assert snapshot["manifest"]["psychometric_count"] == 1
    assert snapshot["manifest"]["slow_state_count"] == 1


def test_offline_replay_compares_all_families_on_the_same_rolling_splits():
    database = memory_database()
    service = ResearchEvaluationService(database, "Asia/Shanghai")
    person_id = uuid.uuid4()
    items = []
    for day in range(1, 5):
        local_day = date(2030, 1, day)
        forecast_id = f"forecast-{day}"
        observation_id = f"observation-{day}"
        items.extend(
            [
                {
                    "item_type": "forecast",
                    "source_id": forecast_id,
                    "source_version": f"v{day}",
                    "participant_id": person_id,
                    "local_date": local_day,
                    "source_hash": f"f{day}",
                    "metadata": {
                        "curve": [
                            {
                                "time": "09:00",
                                "stress_0_10": 4.0 + day * 0.5,
                                    "vitality_0_10": 7.0,
                                    "workload": 0.15 * day,
                                    "recovery_resource": 0.03 * day,
                                    "continuous_load_factor": 0.05 * day,
                            }
                        ]
                    },
                },
                {
                    "item_type": "observation",
                    "source_id": observation_id,
                    "source_version": "observation.v1",
                    "participant_id": person_id,
                    "local_date": local_day,
                    "source_hash": f"o{day}",
                    "metadata": {
                        "payload": {
                            "stress_0_10": 4.2 + day * 0.6,
                            "energy_0_10": 7.0,
                        },
                    },
                },
                {
                    "item_type": "match_source",
                    "source_id": observation_id,
                    "source_version": "match.v1",
                    "participant_id": person_id,
                    "local_date": local_day,
                    "source_hash": f"m{day}",
                    "metadata": {
                        "participant_id": str(person_id),
                        "local_date": local_day.isoformat(),
                        "forecast_id": forecast_id,
                        "forecast_version": f"v{day}",
                        "observation_id": observation_id,
                        "observed_at": f"2030-01-0{day}T09:00:00+08:00",
                        "predicted_stress": 4.0 + day * 0.5,
                        "actual_stress": 4.2 + day * 0.6,
                        "residual": 0.2 + day * 0.1,
                        "prediction_lower": 3.0,
                        "prediction_upper": 8.0,
                        "context": {"forecast_point_time": "09:00"},
                    },
                },
            ]
        )
    items.append(
        {
            "item_type": "psychometric",
            "source_id": "brs-day-4",
            "source_version": "BRS.1",
            "participant_id": person_id,
            "local_date": date(2030, 1, 4),
            "source_hash": "brs4",
            "metadata": {
                "instrument_name": "BRS",
                "scores": {"mean": 5.0},
                "administered_at": "2030-01-04T00:00:00+00:00",
                "created_at": "2030-01-04T00:00:00+00:00",
            },
        }
    )
    result = service._offline_model_comparison(
        items,
        participant_id=None,
        requested_family="all",
        config={"manifest_hash": "frozen"},
    )
    assert result["status"] == "completed"
    assert result["rolling_origin"]["split_count"] == 2
    assert set(result["comparison"]) == set(MODEL_FAMILIES)
    assert set(result["promotion"]) == set(MODEL_FAMILIES) - {"current_m0"}
    assert all(
        value["sample_count"] == 2
        for value in result["comparison"].values()
    )
    assert result["comparison"]["m1"]["observable_support"]["supported"] is True
    assert result["comparison"]["m2"]["observable_support"]["supported"] is False
    assert result["promotion"]["m2"]["checks"]["observable_support"] is False
    assert result["parameter_history"][0]["trait_resilience_prior"] is None
    assert result["parameter_history"][1]["trait_resilience_prior"] == 1.0
