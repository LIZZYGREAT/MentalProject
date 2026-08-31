from datetime import date, datetime, timezone
import uuid

import pytest

from app.models import (
    LearnedModelProfile,
    ModelEvaluationRun,
    ModelPromotionDecision,
)
from app.repositories import (
    ForecastSnapshotRepository,
    LearnedProfileRepository,
    ObservationRepository,
    ParticipantSlowStateRepository,
    PsychometricAssessmentRepository,
)
from app.repositories import promotion_parameters_hash
from app.services.forecast_coordinator import (
    enforce_promoted_model_selection,
    production_model_identity,
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
from app.services.profile_calibration import ProfileCalibrationService
from app.services.model_comparison import PROMOTION_GATE_VERSION
from app.services.model_promotion import ModelPromotionService
from app.services.research_evaluation import EVALUATION_CODE_VERSION
from app.services.stage4_candidate_replay import Stage4CandidateReplayService
from entity.user import User
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
        "m0": model.predict(
            profile=_profile("m0"),
            observations=[],
            calendar_events=_calendar(),
            local_date="2030-01-15",
        ),
        **{
            variant: model.predict_candidate(
                model_variant=variant,
                candidate_params={},
                observations=[],
                calendar_events=_calendar(),
                local_date="2030-01-15",
            )
            for variant in ("wm0", "m1", "m2", "m3")
        },
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
    assert results["m0"].model_version == "mindflow-ctssm-runtime-v7"
    assert {
        results[variant].model_version for variant in ("wm0", "m1", "m2", "m3")
    } == {"mindflow-ctssm-runtime-v8"}
    assert len({result.model_spec_version for result in results.values()}) == 5

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
    assert trait_resilience_prior({"total": 6.0}) == 0.0
    assert trait_resilience_prior({"total": 18.0}) == 0.5
    assert trait_resilience_prior({"total": 30.0}) == 1.0
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


def test_ridge_covariance_uncertainty_and_identifiability_are_auditable():
    identified_samples = [
        {
            "actual_stress": 4.0 + workload * 3.0 - recovery * 2.0,
            "workload": workload,
            "recovery": recovery,
        }
        for workload, recovery in (
            (0.1, 0.2),
            (0.2, 0.8),
            (0.4, 0.1),
            (0.5, 0.6),
            (0.7, 0.3),
            (0.8, 0.9),
            (0.9, 0.4),
            (0.3, 0.7),
        )
    ]
    identified = estimate_reactivity_and_recovery(identified_samples)
    standard_errors = [
        identified["uncertainty"][name]["std_error"]
        for name in (
            "stress_baseline_0_10",
            "workload_reactivity_beta",
            "recovery_beta",
        )
    ]
    assert identified["uncertainty_method"] == "ridge-posterior-covariance.v1"
    assert identified["ridge_lambda"] == 0.35
    assert identified["identifiability_status"] == "identified"
    assert len({round(value, 8) for value in standard_errors}) == 3

    low_variance = estimate_reactivity_and_recovery(
        [
            {
                "actual_stress": 5.0 + (index % 3) * 0.1,
                "workload": 0.5 + (index % 2) * 0.0001,
                "recovery": 0.4 + (index % 3) * 0.0001,
            }
            for index in range(12)
        ]
    )
    assert low_variance["identifiability_status"] == "not_identified"
    assert low_variance["uncertainty"]["workload_reactivity_beta"][
        "std_error"
    ] > identified["uncertainty"]["workload_reactivity_beta"]["std_error"]

    collinear = estimate_reactivity_and_recovery(
        [
            {
                "actual_stress": 4.0 + value,
                "workload": value,
                "recovery": value,
            }
            for value in (0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9)
        ]
    )
    assert collinear["identifiability_status"] == "not_identified"
    assert collinear["design_condition_number"] >= 1e6

    clipped = estimate_reactivity_and_recovery(
        [
            {
                "actual_stress": 8.0 - 5.0 * workload + 2.0 * recovery,
                "workload": workload,
                "recovery": recovery,
            }
            for workload, recovery in (
                (0.1, 0.2),
                (0.2, 0.8),
                (0.4, 0.1),
                (0.5, 0.6),
                (0.7, 0.3),
                (0.8, 0.9),
                (0.9, 0.4),
                (0.3, 0.7),
            )
        ]
    )
    assert clipped["boundary_clipped"] is True
    assert clipped["workload_reactivity_beta"] == 0.0


def test_historical_calibration_uses_only_brs_known_by_through_date():
    database = memory_database()
    person = participant(database, "BRS-AS-OF")
    psychometrics = PsychometricAssessmentRepository(database)
    psychometrics.record(
        person.id,
        instrument_name="BRS",
        instrument_version="1",
        language="zh-CN",
        raw_items={},
        scores={"total": 18},
        administered_at=datetime(2030, 1, 5, tzinfo=timezone.utc),
    )
    psychometrics.record(
        person.id,
        instrument_name="BRS",
        instrument_version="1",
        language="zh-CN",
        raw_items={},
        scores={"total": 30},
        administered_at=datetime(2030, 1, 15, tzinfo=timezone.utc),
    )
    forecasts = ForecastSnapshotRepository(database)
    forecasts.save(
        person.id,
        date(2030, 1, 10),
        calendar_revision="c",
        semantic_revision="s",
        observation_revision="o",
        algorithm_version="a",
        forecast_version="v",
        semantic_status="complete",
        semantic_input=[],
        curve=[
            {
                "time": "09:00",
                "stress_0_10": 5.0,
                "workload": 0.5,
                "recovery_resource": 0.2,
            }
        ],
        peaks=[],
        warning_windows=[],
        output={},
    )
    observations = ObservationRepository(database)
    observations.add(
        person.id,
        "checkin",
        {"stress_0_10": 6.0},
        observed_at=datetime(2030, 1, 10, 9, tzinfo=timezone.utc),
        source_message_id="brs-as-of",
    )
    service = ProfileCalibrationService(
        observations,
        forecasts,
        LearnedProfileRepository(database),
        "Asia/Shanghai",
        psychometrics=psychometrics,
    )
    service.MIN_DAYS = 1
    service.MIN_MATCHED_SAMPLES = 1
    result = service.maybe_calibrate(person.id, through=date(2030, 1, 10))

    assert result["status"] == "calibrated"
    assert result["learned_profile"]["parameters"]["ctssm_params"][
        "trait_resilience_prior"
    ] == 0.5


def test_historical_calibration_excludes_observation_created_after_through():
    class Observations:
        def recent(self, *_args, **_kwargs):
            return [
                {
                    "id": "late",
                    "observed_at": "2030-01-10T09:00:00+08:00",
                    "created_at": "2030-01-15T09:00:00+08:00",
                    "payload": {"stress_0_10": 9.0},
                },
                {
                    "id": "causal",
                    "observed_at": "2030-01-10T10:00:00+08:00",
                    "created_at": "2030-01-10T10:01:00+08:00",
                    "payload": {"stress_0_10": 6.0},
                },
            ]

    class Forecasts:
        def current_at(self, *_args, **_kwargs):
            return {
                "id": "forecast",
                "forecast_version": "v1",
                "generated_at": "2030-01-09T00:00:00+00:00",
                "curve": [{"time": "10:00", "stress_0_10": 5.0}],
                "output": {},
            }

    service = ProfileCalibrationService(
        Observations(), Forecasts(), object(), "Asia/Shanghai"
    )
    samples = service.causal_samples("participant", through=date(2030, 1, 10))

    assert [sample["observation_id"] for sample in samples] == ["causal"]


def test_candidate_replay_never_assimilates_target_future_or_late_known_ema():
    target = datetime(2030, 1, 10, 10, tzinfo=timezone.utc)
    history = [
        {
            "observation_type": "checkin",
            "observed_at": "2030-01-10T09:00:00+00:00",
            "created_at": "2030-01-10T09:01:00+00:00",
            "payload": {"stress_0_10": 5.0},
        },
        {
            "observation_type": "checkin",
            "observed_at": target.isoformat(),
            "created_at": target.isoformat(),
            "payload": {"stress_0_10": 9.0},
        },
        {
            "observation_type": "checkin",
            "observed_at": "2030-01-10T09:30:00+00:00",
            "created_at": "2030-01-10T10:30:00+00:00",
            "payload": {"stress_0_10": 8.0},
        },
        {
            "observation_type": "checkin",
            "observed_at": "2030-01-10T11:00:00+00:00",
            "created_at": "2030-01-10T11:00:00+00:00",
            "payload": {"stress_0_10": 7.0},
        },
    ]

    known = Stage4CandidateReplayService._known_observations(history, target)

    assert len(known) == 1
    assert known[0]["payload"]["stress_0_10"] == 5.0


def test_rolling_origin_excludes_late_backfilled_training_ema_by_knowledge_time():
    participant_id = uuid.uuid4()

    def replay(day_one_created_at: str):
        items = []
        for day in range(1, 4):
            local_day = date(2030, 1, day)
            observation_id = f"knowledge-observation-{day}"
            forecast_id = f"knowledge-forecast-{day}"
            observed_at = f"2030-01-0{day}T09:00:00+08:00"
            created_at = (
                day_one_created_at
                if day == 1
                else f"2030-01-0{day}T09:01:00+08:00"
            )
            items.extend(
                [
                    {
                        "item_type": "forecast",
                        "source_id": forecast_id,
                        "source_version": "v1",
                        "participant_id": participant_id,
                        "local_date": local_day,
                        "source_hash": f"forecast-{day}",
                        "metadata": {
                            "initial_state": {
                                "stress_0_10": 4.0,
                                "vitality_0_10": 7.0,
                            },
                            "initial_state_revision": f"initial-{day}",
                            "curve": [
                                {
                                    "time": "09:00",
                                    "stress_0_10": 4.0 + day,
                                    "vitality_0_10": 7.0,
                                    "workload": day / 4.0,
                                    "recovery_resource": (4 - day) / 4.0,
                                }
                            ],
                        },
                    },
                    {
                        "item_type": "observation",
                        "source_id": observation_id,
                        "source_version": "observation.v1",
                        "participant_id": participant_id,
                        "local_date": local_day,
                        "source_hash": f"observation-{day}",
                        "metadata": {
                            "observation_type": "checkin",
                            "observed_at": observed_at,
                            "created_at": created_at,
                            "payload": {
                                "stress_0_10": 4.5 + day,
                                "energy_0_10": 7.0,
                            },
                        },
                    },
                    {
                        "item_type": "match_source",
                        "source_id": observation_id,
                        "source_version": "match.v2",
                        "participant_id": participant_id,
                        "local_date": local_day,
                        "source_hash": f"match-{day}",
                        "metadata": {
                            "forecast_id": forecast_id,
                            "observation_id": observation_id,
                            "observed_at": observed_at,
                            "predicted_stress": 4.0 + day,
                            "actual_stress": 4.5 + day,
                            "prediction_lower": 3.0,
                            "prediction_upper": 9.0,
                            "context": {"forecast_point_time": "09:00"},
                        },
                    },
                ]
            )
        return Stage4CandidateReplayService("Asia/Shanghai").compare(
            items,
            participant_id=None,
            requested_family="all",
            config={},
        )

    late = replay("2030-01-04T09:00:00+08:00")
    causal = replay("2030-01-01T09:01:00+08:00")

    assert late["parameter_history"][0]["training_sample_count"] == 1
    assert causal["parameter_history"][0]["training_sample_count"] == 2
    assert late["rolling_origin"]["splits"][0]["origin_cutoff"] == (
        "2030-01-02T16:00:00+00:00"
    )


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
                        "model_variant": "m1",
                        "initial_state": {
                            "stress_0_10": 4.0,
                            "vitality_0_10": 7.0,
                        },
                        "initial_state_revision": f"initial-{day}",
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
                        "observed_at": f"2030-01-0{day}T09:00:00+08:00",
                        "created_at": f"2030-01-0{day}T09:01:00+08:00",
                        "observation_type": "checkin",
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
                        "predicted_stress": 9.0,
                        "actual_stress": 4.2 + day * 0.6,
                        "residual": 0.2 + day * 0.1,
                        "prediction_lower": 3.0,
                        "prediction_upper": 8.0,
                        "context": {
                            "forecast_point_time": "09:00",
                            "model_variant": "m1",
                        },
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
    assert {
        "post_event_exposure_count",
        "post_event_ema_count",
        "stress_persistence_transition_count",
        "participant_count",
        "day_count",
    } <= set(result["comparison"]["m2"]["observable_support"]["counts"])
    assert {
        "sustained_workload_episode_count",
        "continuous_load_level_count",
        "post_load_recovery_transition_count",
        "vitality_observation_count",
        "participant_count",
        "day_count",
    } <= set(result["comparison"]["m3"]["observable_support"]["counts"])
    assert result["promotion"]["m2"]["checks"]["observable_support"] is False
    assert result["parameter_history"][0]["trait_resilience_prior"] is None
    assert result["parameter_history"][1]["trait_resilience_prior"] is None
    assert all(
        row["target_observation_assimilated"] is False
        and row["interval_source"] == "LatentUncertainty/prediction_interval"
        and row["trajectory_point_count"] == 288
        for row in result["replay_audit"]
    )
    assert all(
        result["comparison"][family]["replay_engine"]
        == "stage4-real-ctssm-replay.v3"
        for family in ("workload_aware_m0", "m1", "m2", "m3")
    )
    participant_key = str(person_id)
    final_fit = [
        row
        for row in result["parameter_history"]
        if row["participant_id"] == participant_key
    ][-1]
    uncertainty = result["candidate_parameter_uncertainty"][participant_key]
    assert uncertainty["S_star_init"]["std_error"] == pytest.approx(
        final_fit["uncertainty"]["stress_baseline_0_10"]["std_error"] * 10.0
    )
    assert uncertainty["ctssm_params"]["workload_stress_gain"][
        "std_error"
    ] == pytest.approx(
        final_fit["uncertainty"]["workload_reactivity_beta"]["std_error"]
        * 10.0
    )
    assert "stress_reactivity_per_hour" not in result["candidate_parameters"][
        participant_key
    ]["ctssm_params"]
    assert result["config"]["initial_state_provenance_complete"] is True
    assert result["config"]["candidate_latent_initialization"]["version"] == (
        "candidate-latent-initialization.v1"
    )
    assert result["historical_production"]["mae"] == 2.7
    assert result["comparison"]["current_m0"]["mae"] != 2.7
    m0_audit = [
        row for row in result["replay_audit"] if row["family"] == "current_m0"
    ]
    assert m0_audit
    assert all(row["replayed_model_variant"] == "m0" for row in m0_audit)
    for target in {row["target"] for row in m0_audit}:
        same_target = [row for row in result["replay_audit"] if row["target"] == target]
        assert len({row["initial_state_revision"] for row in same_target}) == 1
        assert len({row["daily_peak_origin"] for row in same_target}) == 1


def test_production_candidate_requires_durable_promotion_provenance():
    assert User(
        params={
            "model_selection": {
                "active_variant": "m1",
                "status": "research_candidate_run",
            }
        }
    ).params["model_selection"]["active_variant"] == "m0"

    database = memory_database()
    person = participant(database, "PROMOTION-PROVENANCE")
    learned = LearnedProfileRepository(database)
    unproven = {
        "S_star_init": 50.0,
        "ctssm_params": {"workload_stress_gain": 25.0},
        "model_selection": {
            "active_variant": "m1",
            "status": "retained_from_empirical_evidence",
        },
    }
    learned.save(
        person.id,
        parameters=unproven,
        uncertainty={key: {"std_error": 0.0} for key in unproven},
        sample_count=10,
        day_count=3,
        confidence=1.0,
        window_start=date(2030, 1, 1),
        window_end=date(2030, 1, 3),
        model_version="candidate-without-proof",
        validation_status="validated",
    )
    assert learned.runtime_active(person.id) is None

    research = ResearchEvaluationService(database, "Asia/Shanghai")
    snapshot = research.create_dataset_snapshot(
        date_start=date(2030, 1, 1),
        date_end=date(2030, 1, 3),
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2030, 1, 4, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2030, 1, 4, tzinfo=timezone.utc),
    )
    candidate_params = {
        "S_star_init": 48.0,
        "ctssm_params": {
            "workload_stress_gain": 24.0,
            "recovery_stress_gain": 16.0,
        },
    }
    candidate_uncertainty = {
        "S_star_init": {"std_error": 1.2},
        "ctssm_params": {
            "workload_stress_gain": {"std_error": 2.1},
            "recovery_stress_gain": {"std_error": 1.7},
        },
    }
    candidate_evidence = {
        "sample_count": 12,
        "day_count": 3,
        "transition_count": 4,
        "identifiability_status": "weak",
    }
    with database.session() as session:
        run = ModelEvaluationRun(
            dataset_snapshot_id=uuid.UUID(snapshot["id"]),
            model_version="m1",
            evaluation_mode="offline_replay",
            evaluation_code_version=EVALUATION_CODE_VERSION,
            participant_id=person.id,
            metrics_json={
                "config": {
                    "manifest_hash": snapshot["manifest"]["manifest_hash"],
                    "initial_state_provenance_complete": True,
                },
                "comparison": {"m1": {"mae": 0.9, "sample_count": 12}},
                "promotion": {
                    "m1": {
                        "passed": True,
                        "gate_version": PROMOTION_GATE_VERSION,
                    }
                },
                    "candidate_parameters": {
                        str(person.id): candidate_params,
                    },
                    "candidate_parameter_uncertainty": {
                        str(person.id): candidate_uncertainty,
                    },
                    "candidate_parameter_evidence": {
                        str(person.id): candidate_evidence,
                    },
            },
            status="completed",
        )
        session.add(run)
        session.flush()
        run_id = run.id
        cohort_run = ModelEvaluationRun(
            dataset_snapshot_id=uuid.UUID(snapshot["id"]),
            model_version="m1",
            evaluation_mode="offline_replay",
            evaluation_code_version=EVALUATION_CODE_VERSION,
            participant_id=None,
            metrics_json=dict(run.metrics_json),
            status="completed",
        )
        session.add(cohort_run)
        session.flush()
        cohort_run_id = cohort_run.id

    other = participant(database, "PROMOTION-OTHER")
    promotion_service = ModelPromotionService(database, "Asia/Shanghai")
    with pytest.raises(
        ValueError,
        match="participant promotion requires participant-specific evaluation run",
    ):
        promotion_service.promote_candidate(
            cohort_run_id, participant_id=person.id, model_family="m1"
        )
    with pytest.raises(
        ValueError,
        match="participant promotion requires participant-specific evaluation run",
    ):
        promotion_service.promote_candidate(
            run_id, participant_id=other.id, model_family="m1"
        )

    decision = promotion_service.promote_candidate(
        run_id, participant_id=person.id, model_family="m1"
    )
    active = learned.runtime_active(person.id)

    assert decision["status"] == "retained_from_empirical_evidence"
    assert active["parameters"]["model_selection"]["active_variant"] == "m1"
    assert active["parameters"]["model_selection"][
        "promotion_decision_id"
    ] == decision["id"]
    assert active["confidence"] < 1.0
    assert "model_selection" not in active["uncertainty"]
    assert "stress_reactivity_per_hour" not in active["parameters"]["ctssm_params"]
    learned.save(
        person.id,
        parameters={"S_star_init": 99.0},
        uncertainty={},
        sample_count=1,
        day_count=1,
        confidence=0.1,
        window_start=date(2030, 1, 3),
        window_end=date(2030, 1, 3),
        model_version="newer-research-candidate",
        validation_status="candidate",
    )
    dashboard = research.model_comparison_dashboard(person.id)
    assert dashboard["current_model"] == "m1"
    assert dashboard["latest_run"]["scope"] == "participant"
    assert dashboard["latest_run"]["id"] == str(run_id)
    assert dashboard["cohort_latest_run"]["scope"] == "cohort"


def test_production_model_identity_distinguishes_m0_m1_and_m3():
    model = AssessmentModel("Asia/Shanghai")
    m0 = production_model_identity({"model_params": {}}, model)

    def promoted(variant: str) -> dict:
        return production_model_identity(
            {
                "model_params": {
                    "model_selection": {
                        "active_variant": variant,
                        "status": "retained_from_empirical_evidence",
                        "promotion_decision_id": f"decision-{variant}",
                        "parameters_hash": f"hash-{variant}",
                    }
                }
            },
            model,
        )

    m1, m3 = promoted("m1"), promoted("m3")
    assert m0["engine_version"] == "mindflow-ctssm-runtime-v7"
    assert m1["engine_version"] == m3["engine_version"] == (
        "mindflow-ctssm-runtime-v8"
    )
    assert {identity["model_variant"] for identity in (m0, m1, m3)} == {
        "m0",
        "m1",
        "m3",
    }
    assert len({identity["model_spec_version"] for identity in (m0, m1, m3)}) == 3


def test_participant_promotion_rolls_back_decision_and_profile_atomically(
    monkeypatch,
):
    database = memory_database()
    person = participant(database, "PROMOTION-ATOMIC")
    research = ResearchEvaluationService(database, "Asia/Shanghai")
    snapshot = research.create_dataset_snapshot(
        date_start=date(2030, 2, 1),
        date_end=date(2030, 2, 3),
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2030, 2, 4, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2030, 2, 4, tzinfo=timezone.utc),
    )
    candidate_params = {
        "S_star_init": 50.0,
        "ctssm_params": {
            "workload_stress_gain": 28.0,
            "recovery_stress_gain": 14.0,
        },
    }
    with database.session() as session:
        run = ModelEvaluationRun(
            dataset_snapshot_id=uuid.UUID(snapshot["id"]),
            model_version="m1",
            evaluation_mode="offline_replay",
            evaluation_code_version=EVALUATION_CODE_VERSION,
            participant_id=person.id,
            metrics_json={
                "config": {
                    "manifest_hash": snapshot["manifest"]["manifest_hash"],
                    "initial_state_provenance_complete": True,
                },
                "comparison": {"m1": {"mae": 0.8}},
                "promotion": {
                    "m1": {
                        "passed": True,
                        "gate_version": PROMOTION_GATE_VERSION,
                    }
                },
                "candidate_parameters": {str(person.id): candidate_params},
                "candidate_parameter_uncertainty": {
                    str(person.id): {
                        "S_star_init": {"std_error": 1.0},
                        "ctssm_params": {
                            "workload_stress_gain": {"std_error": 2.0},
                            "recovery_stress_gain": {"std_error": 1.5},
                        },
                    }
                },
                "candidate_parameter_evidence": {
                    str(person.id): {
                        "sample_count": 12,
                        "day_count": 3,
                        "transition_count": 4,
                        "identifiability_status": "identified",
                    }
                },
            },
            status="completed",
        )
        session.add(run)
        session.flush()
        run_id = run.id

    service = ModelPromotionService(database, "Asia/Shanghai")
    real_save = service.learned_profiles.save_in_session

    def fail_profile_insert(*_args, **_kwargs):
        raise RuntimeError("simulated profile insert failure")

    monkeypatch.setattr(
        service.learned_profiles, "save_in_session", fail_profile_insert
    )
    with pytest.raises(RuntimeError, match="simulated profile insert failure"):
        service.promote_candidate(
            run_id, participant_id=person.id, model_family="m1"
        )
    with database.session() as session:
        assert session.query(ModelPromotionDecision).count() == 0
        assert session.query(LearnedModelProfile).count() == 0

    monkeypatch.setattr(service.learned_profiles, "save_in_session", real_save)
    result = service.promote_candidate(
        run_id, participant_id=person.id, model_family="m1"
    )
    with database.session() as session:
        assert session.query(ModelPromotionDecision).count() == 1
        assert session.query(LearnedModelProfile).count() == 1
    assert result["learned_profile"] is not None


def test_explicit_profile_override_cannot_mutate_promoted_candidate():
    candidate = {
        "S_star_init": 50.0,
        "ctssm_params": {"workload_stress_gain": 25.0},
    }
    selection = {
        "active_variant": "m1",
        "status": "retained_from_empirical_evidence",
        "promotion_decision_id": str(uuid.uuid4()),
        "parameters_hash": promotion_parameters_hash(candidate),
    }
    learned = {"parameters": {**candidate, "model_selection": selection}}
    valid = enforce_promoted_model_selection(
        {"model_params": {**candidate, "model_selection": selection}}, learned
    )
    overridden = enforce_promoted_model_selection(
        {
            "model_params": {
                **candidate,
                "ctssm_params": {"workload_stress_gain": 99.0},
                "model_selection": selection,
            }
        },
        learned,
    )

    assert valid["model_params"]["model_selection"]["active_variant"] == "m1"
    assert overridden["model_params"]["model_selection"] == {
        "active_variant": "m0",
        "status": "promotion_provenance_missing",
    }
