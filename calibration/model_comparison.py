"""Out-of-time comparison for the paper-defined nested stress models.

The module deliberately distinguishes an executable candidate from an
empirically retained state.  It can run on supplied longitudinal samples now,
but it will not promote M1--M3 when posterior/identifiability evidence is
missing.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np

from algorithm.dynamic_state_model import (
    MODEL_VARIANTS,
    build_event_assessments,
    calculate_dynamic_inputs,
    model_variant_metadata,
)
from algorithm.time_utils import time_to_minutes
from calibration.simulation_runner import run_simulation_for_calibration
from utils.event_factory import EventFactory


NESTED_SEQUENCE = ("m0", "m1", "m2", "m3")
Z_90 = 1.6448536269514722


@dataclass(frozen=True)
class Observation:
    date: str
    time: str
    stress: Optional[float] = None
    vitality: Optional[float] = None
    cognition: Optional[float] = None


def run_nested_model_comparison(
    samples: Iterable[Dict[str, Any]],
    base_params: Mapping[str, Any],
    *,
    variants: Sequence[str] = NESTED_SEQUENCE,
    holdout_fraction: float = 0.30,
) -> Dict[str, Any]:
    """Evaluate complete held-out dates; adjacent points are never shuffled."""

    sample_list = sorted(
        [dict(item) for item in samples],
        key=lambda item: str(item.get("date") or ""),
    )
    if not sample_list:
        raise ValueError("model comparison requires at least one dated sample")
    if any(not item.get("date") for item in sample_list):
        raise ValueError("every comparison sample requires a date")

    dates = sorted({str(item["date"]) for item in sample_list})
    holdout_count = max(1, int(math.ceil(len(dates) * holdout_fraction)))
    if len(dates) <= 1:
        train_dates: List[str] = []
        test_dates = dates
    else:
        holdout_count = min(len(dates) - 1, holdout_count)
        train_dates = dates[:-holdout_count]
        test_dates = dates[-holdout_count:]
    train_samples = [item for item in sample_list if item["date"] in train_dates]
    test_samples = [item for item in sample_list if item["date"] in test_dates]

    candidate_reports: Dict[str, Dict[str, Any]] = {}
    for raw_variant in variants:
        variant = str(raw_variant).lower()
        if variant not in MODEL_VARIANTS:
            raise ValueError(f"unsupported nested model variant: {raw_variant}")
        params = deepcopy(dict(base_params))
        params["model_family"] = MODEL_VARIANTS[variant]["canonical"]
        params["model_selection"] = {
            **dict(params.get("model_selection", {}) or {}),
            "active_variant": variant,
            "status": "research_candidate_run",
        }
        train_metrics = _evaluate_samples(train_samples, params, variant)
        test_metrics = _evaluate_samples(test_samples, params, variant)
        candidate_reports[variant] = {
            **model_variant_metadata(variant),
            "train": train_metrics,
            "test": test_metrics,
            "parameter_source": "versioned_group_defaults_or_declared_priors",
            "posterior_diagnostics": "not_available",
        }

    baseline_report = evaluate_discrete_baselines(train_samples, test_samples)
    event_regression_report = evaluate_event_regression_baseline(
        train_samples,
        test_samples,
    )
    coupling_reports = _evaluate_m1_couplings(test_samples, base_params)
    hypothesis_checks = _evaluate_hypothesis_ablations(
        test_samples,
        base_params,
        candidate_reports,
    )
    event_time_shape = {
        "flexible_piecewise_discovery_on_train_dates": discover_event_time_shape(
            train_samples
        ),
        "held_out_kernel_comparison": _evaluate_kernel_modes(
            test_samples,
            base_params,
        ),
        "selection_rule": (
            "Only simplify to an exponential kernel when held-out prediction and "
            "the discovered monotone shape both support it."
        ),
    }
    recommendation = select_retained_variant(
        candidate_reports,
        test_day_count=len(test_dates),
        base_params=base_params,
    )
    return {
        "schema_version": "nested_model_comparison.v2",
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "split": {
            "method": "complete_date_holdout",
            "train_dates": train_dates,
            "test_dates": test_dates,
            "holdout_fraction": holdout_fraction,
            "adjacent_points_randomly_split": False,
        },
        "candidate_reports": candidate_reports,
        "model_sequence": {
            "m0_constant": baseline_report.get("individual_mean"),
            "m0_event_regression": event_regression_report,
            "m0_continuous_time": candidate_reports.get("m0"),
            "m1": candidate_reports.get("m1"),
            "m2": candidate_reports.get("m2"),
            "m3": candidate_reports.get("m3"),
            "m4_hierarchical": evaluate_hierarchical_m4_readiness(sample_list),
        },
        "discrete_baselines": baseline_report,
        "m1_coupling_reports": coupling_reports,
        "hypothesis_checks": hypothesis_checks,
        "event_time_shape": event_time_shape,
        "recommendation": recommendation,
        "limitations": [
            "No posterior-identifiability claim is made without a fitted probabilistic model.",
            "A result based only on synthetic schedules is an engineering check, not empirical retention evidence.",
            "Missing or stress-triggered-only EMA may bias the comparison.",
        ],
    }


def _candidate_params(
    base_params: Mapping[str, Any],
    variant: str,
    *,
    coupling: Optional[str] = None,
) -> Dict[str, Any]:
    params = deepcopy(dict(base_params))
    params["model_family"] = MODEL_VARIANTS[variant]["canonical"]
    params["model_selection"] = {
        **dict(params.get("model_selection", {}) or {}),
        "active_variant": variant,
        "status": "research_candidate_run",
    }
    if coupling is not None:
        params.setdefault("ctssm_params", {})[
            "stress_vitality_coupling"
        ] = coupling
    return params


def _evaluate_m1_couplings(
    test_samples: Sequence[Dict[str, Any]],
    base_params: Mapping[str, Any],
) -> Dict[str, Any]:
    modes = {
        "m1-a": "none",
        "m1-b": "v_to_s",
        "m1-c": "s_to_v",
        "m1-d": "bidirectional",
    }
    return {
        name: {
            "coupling": coupling,
            "test": _evaluate_samples(
                test_samples,
                _candidate_params(base_params, "m1", coupling=coupling),
                "m1",
            ),
            "status": "candidate_not_retained_without_out_of_time_and_identifiability_evidence",
        }
        for name, coupling in modes.items()
    }


def discover_event_time_shape(
    train_samples: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Exploratory relative-time bins using train dates only, without causality claims."""
    bins: Dict[str, List[float]] = {
        "pre_120_to_60": [],
        "pre_60_to_0": [],
        "active": [],
        "post_0_to_60": [],
        "post_60_to_120": [],
    }
    for sample in train_samples:
        observations = [obs for obs in observations_from_sample(sample) if obs.stress is not None]
        if not observations:
            continue
        day_center = _mean([float(obs.stress) for obs in observations])
        if day_center is None:
            continue
        for event in sample.get("events", []) or []:
            if not isinstance(event, Mapping):
                continue
            start = _time_part(event.get("start_time") or event.get("start"))
            end = _time_part(event.get("end_time") or event.get("end"))
            if not start or not end:
                continue
            start_minute = time_to_minutes(start)
            end_minute = time_to_minutes(end)
            if end_minute <= start_minute:
                end_minute += 1440
            for observation in observations:
                minute = time_to_minutes(observation.time)
                if minute < start_minute - 120 or minute > end_minute + 120:
                    continue
                residual = float(observation.stress) - day_center
                if start_minute - 120 <= minute < start_minute - 60:
                    bins["pre_120_to_60"].append(residual)
                elif start_minute - 60 <= minute < start_minute:
                    bins["pre_60_to_0"].append(residual)
                elif start_minute <= minute <= end_minute:
                    bins["active"].append(residual)
                elif end_minute < minute <= end_minute + 60:
                    bins["post_0_to_60"].append(residual)
                elif end_minute + 60 < minute <= end_minute + 120:
                    bins["post_60_to_120"].append(residual)
    estimates = {
        name: {"count": len(values), "mean_centered_stress": _mean(values)}
        for name, values in bins.items()
    }
    observed_bins = sum(int(item["count"] > 0) for item in estimates.values())
    return {
        "method": "piecewise_relative_time_bins",
        "data_scope": "training_dates_only",
        "estimates": estimates,
        "status": (
            "exploratory_shape_available"
            if observed_bins >= 3
            else "insufficient_relative_time_coverage"
        ),
        "interpretation": "descriptive_within-day-centered_shape_not_causal_effect",
    }


def _evaluate_kernel_modes(
    test_samples: Sequence[Dict[str, Any]],
    base_params: Mapping[str, Any],
) -> Dict[str, Any]:
    reports = {}
    for mode in ("piecewise", "exponential"):
        samples = deepcopy(list(test_samples))
        for sample in samples:
            for event in sample.get("events", []) or []:
                if not isinstance(event, dict):
                    continue
                metadata = event.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    event["metadata"] = metadata
                metadata["kernel_mode"] = mode
                event["kernel_mode"] = mode
        reports[mode] = _evaluate_samples(
            samples,
            _candidate_params(base_params, "m0"),
            "m0",
        )
    piecewise_mae = reports["piecewise"].get("stress_mae")
    exponential_mae = reports["exponential"].get("stress_mae")
    return {
        **reports,
        "exponential_supported_by_held_out_error": (
            piecewise_mae is not None
            and exponential_mae is not None
            and float(exponential_mae) <= float(piecewise_mae)
        ),
        "automatic_kernel_change": False,
    }
def _evaluate_hypothesis_ablations(
    test_samples: Sequence[Dict[str, Any]],
    base_params: Mapping[str, Any],
    reports: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    m0_full = reports.get("m0", {}).get("test", {})
    category_only_samples = _strip_event_appraisal(test_samples)
    category_only = _evaluate_samples(
        category_only_samples,
        _candidate_params(base_params, "m0"),
        "m0",
    )

    symmetric_params = _candidate_params(base_params, "m0")
    ct_cfg = symmetric_params.setdefault("ctssm_params", {})
    shared_rate = 0.5 * (
        float(ct_cfg.get("stress_reactivity_per_hour", 1.55))
        + float(ct_cfg.get("stress_recovery_per_hour", 0.68))
    )
    ct_cfg["stress_reactivity_per_hour"] = shared_rate
    ct_cfg["stress_recovery_per_hour"] = shared_rate
    symmetric = _evaluate_samples(test_samples, symmetric_params, "m0")

    no_pre_params = _candidate_params(base_params, "m2")
    no_pre_params.setdefault("ctssm_params", {})["anticipation_gain_per_hour"] = 0.0
    no_pre = _evaluate_samples(test_samples, no_pre_params, "m2")

    no_post_params = _candidate_params(base_params, "m2")
    no_post_params.setdefault("ctssm_params", {})["aftermath_gain_per_hour"] = 0.0
    no_post = _evaluate_samples(test_samples, no_post_params, "m2")

    no_sleep_params = _candidate_params(base_params, "m0")
    no_sleep_cfg = no_sleep_params.setdefault("ctssm_params", {})
    for key in (
        "sleep_debt_stress_per_hour",
        "sleep_quality_initial_stress_gain",
        "sleep_quality_event_appraisal_gain",
    ):
        no_sleep_cfg[key] = 0.0
    no_sleep = _evaluate_samples(test_samples, no_sleep_params, "m0")

    high_load = [sample for sample in test_samples if _is_high_load_sample(sample)]
    ordinary = [sample for sample in test_samples if not _is_high_load_sample(sample)]

    def pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> Dict[str, Any]:
        left_mae = left.get("stress_mae")
        right_mae = right.get("stress_mae")
        improvement = None
        if left_mae is not None and right_mae is not None and float(right_mae) > 0:
            improvement = (float(right_mae) - float(left_mae)) / float(right_mae)
        return {
            "full_or_richer": dict(left),
            "ablated_or_simpler": dict(right),
            "relative_stress_mae_improvement": improvement,
            "conclusion": "requires_real_out_of_time_data",
        }

    return {
        "H1_appraisal_over_category": pair(m0_full, category_only),
        "H2_asymmetric_reactivity_recovery": pair(m0_full, symmetric),
        "H3_anticipation_increment": pair(
            reports.get("m2", {}).get("test", {}),
            no_pre,
        ),
        "H4_aftermath_increment": pair(
            reports.get("m2", {}).get("test", {}),
            no_post,
        ),
        "H5_pile_up": {
            "high_load_m3_vs_m2": pair(
                _evaluate_samples(high_load, _candidate_params(base_params, "m3"), "m3"),
                _evaluate_samples(high_load, _candidate_params(base_params, "m2"), "m2"),
            ),
            "high_load_day_count": len(high_load),
            "observable_diagnostics": {
                "event_count_correlation_with_F": reports.get("m3", {})
                .get("test", {})
                .get("mean_fatigue_event_count_pileup_correlation"),
                "response_auc_correlation_with_F": reports.get("m3", {})
                .get("test", {})
                .get("mean_fatigue_response_pileup_correlation"),
            },
        },
        "H6_sleep_context_increment": pair(m0_full, no_sleep),
        "H7_vitality_increment": pair(
            reports.get("m1", {}).get("test", {}),
            reports.get("m0", {}).get("test", {}),
        ),
        "H8_fatigue_specific_to_continuous_load": {
            "high_load": pair(
                _evaluate_samples(high_load, _candidate_params(base_params, "m3"), "m3"),
                _evaluate_samples(high_load, _candidate_params(base_params, "m2"), "m2"),
            ),
            "ordinary_load": pair(
                _evaluate_samples(ordinary, _candidate_params(base_params, "m3"), "m3"),
                _evaluate_samples(ordinary, _candidate_params(base_params, "m2"), "m2"),
            ),
            "conclusion": "M3 should improve mainly in high-load days; real data required",
        },
    }


def _strip_event_appraisal(
    samples: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    stripped = deepcopy(list(samples))
    allowed = {
        "id",
        "event_id",
        "event_type",
        "type",
        "task_type",
        "summary",
        "name",
        "start_time",
        "end_time",
        "start",
        "end",
    }
    for sample in stripped:
        sample["events"] = [
            {key: value for key, value in event.items() if key in allowed}
            for event in sample.get("events", [])
            if isinstance(event, Mapping)
        ]
    return stripped


def _is_high_load_sample(sample: Mapping[str, Any]) -> bool:
    events = [event for event in sample.get("events", []) if isinstance(event, Mapping)]
    if len(events) >= 3:
        return True
    total_minutes = 0
    for event in events:
        start = _time_part(event.get("start_time") or event.get("start"))
        end = _time_part(event.get("end_time") or event.get("end"))
        if start and end:
            total_minutes += max(0, time_to_minutes(end) - time_to_minutes(start))
    return total_minutes >= 240


def _evaluate_samples(
    samples: Sequence[Dict[str, Any]],
    params: Dict[str, Any],
    variant: str,
) -> Dict[str, Any]:
    day_metrics = []
    for sample in samples:
        simulation = run_simulation_for_calibration(
            date_str=str(sample["date"]),
            events_json=sample.get("events", []),
            user_params=params,
            yesterday_state=sample.get("yesterday_state"),
            weave_routines=bool(sample.get("weave_routines", True)),
            observations=sample.get("online_observations", []),
            sleep_context=sample.get("sleep_context"),
        )
        observations = observations_from_sample(sample)
        metrics = evaluate_trajectory_observations(
            simulation["results"],
            observations,
            variant=variant,
        )
        metrics.update(
            _observable_pile_up_diagnostics(
                simulation["results"],
                sample.get("events", []),
                enabled=variant == "m3",
            )
        )
        day_metrics.append(metrics)
    return aggregate_comparison_metrics(day_metrics)


def observations_from_sample(sample: Mapping[str, Any]) -> List[Observation]:
    date = str(sample.get("date") or "")
    observations: List[Observation] = []
    raw_observations = sample.get("observations")
    if isinstance(raw_observations, list):
        for raw in raw_observations:
            if not isinstance(raw, Mapping):
                continue
            payload = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else {}
            merged = {**raw, **payload}
            time = _time_part(merged.get("time") or merged.get("target_time"))
            if not time:
                continue
            observations.append(
                Observation(
                    date=date,
                    time=time,
                    stress=_rating(merged.get("stress", merged.get("stress_0_10"))),
                    vitality=_rating(
                        merged.get(
                            "vitality",
                            merged.get("vitality_0_10", merged.get("energy_0_10")),
                        )
                    ),
                    cognition=_unit_rating(
                        merged.get(
                            "perseverative_cognition",
                            merged.get("perseverative_cognition_0_10"),
                        )
                    ),
                )
            )
    feedback = sample.get("feedback")
    if not observations and isinstance(feedback, Mapping):
        for period, default_time in (
            ("morning", "08:00"),
            ("noon", "13:00"),
            ("evening", "22:00"),
        ):
            stress = _rating(feedback.get(f"stress_{period}"))
            vitality = _rating(
                feedback.get(f"vitality_{period}", feedback.get(f"energy_{period}"))
            )
            if stress is None and vitality is None:
                continue
            observations.append(
                Observation(
                    date=date,
                    time=str(feedback.get(f"{period}_time") or default_time)[:5],
                    stress=stress,
                    vitality=vitality,
                )
            )
    return sorted(observations, key=lambda item: time_to_minutes(item.time))


def evaluate_trajectory_observations(
    results: Sequence[Dict[str, Any]],
    observations: Sequence[Observation],
    *,
    variant: str,
) -> Dict[str, Any]:
    stress_errors: List[float] = []
    stress_squared_errors: List[float] = []
    vitality_errors: List[float] = []
    vitality_squared_errors: List[float] = []
    cognition_errors: List[float] = []
    cognition_squared_errors: List[float] = []
    covered: List[float] = []
    nll_values: List[float] = []
    predicted_stress: List[float] = []
    observed_stress: List[float] = []
    matched_stress: List[tuple[int, float, float]] = []
    for observation in observations:
        row = _closest_row(results, observation.time)
        if row is None:
            continue
        if observation.stress is not None:
            prediction = float(row.get("S", 0.0))
            stress_errors.append(abs(prediction - observation.stress))
            stress_squared_errors.append((prediction - observation.stress) ** 2)
            predicted_stress.append(prediction)
            observed_stress.append(observation.stress)
            matched_stress.append(
                (time_to_minutes(observation.time), prediction, observation.stress)
            )
            interval = row.get("stress_interval_90") or {}
            lower = float(interval.get("lower", prediction))
            upper = float(interval.get("upper", prediction))
            covered.append(float(lower <= observation.stress <= upper))
            sd = max(1e-3, (upper - lower) / (2.0 * Z_90))
            residual = observation.stress - prediction
            nll_values.append(
                0.5 * math.log(2.0 * math.pi * sd * sd)
                + 0.5 * residual * residual / (sd * sd)
            )
        if observation.vitality is not None and variant != "m0":
            vitality_residual = (
                float(row.get("V", row.get("E", 0.0))) - observation.vitality
            )
            vitality_errors.append(abs(vitality_residual))
            vitality_squared_errors.append(vitality_residual ** 2)
        if observation.cognition is not None and variant in {"m2", "m3"}:
            cognition_residual = float(row.get("P", 0.0)) - observation.cognition
            cognition_errors.append(abs(cognition_residual))
            cognition_squared_errors.append(cognition_residual ** 2)
    return {
        "day_count": 1,
        "observation_count": len(observations),
        "stress_observation_count": len(stress_errors),
        "vitality_observation_count": len(vitality_errors),
        "cognition_observation_count": len(cognition_errors),
        "stress_mae": _mean(stress_errors),
        "stress_rmse": (
            math.sqrt(_mean(stress_squared_errors))
            if stress_squared_errors
            else None
        ),
        "vitality_mae": _mean(vitality_errors),
        "vitality_rmse": (
            math.sqrt(_mean(vitality_squared_errors))
            if vitality_squared_errors
            else None
        ),
        "cognition_mae": _mean(cognition_errors),
        "cognition_rmse": (
            math.sqrt(_mean(cognition_squared_errors))
            if cognition_squared_errors
            else None
        ),
        "interval_coverage_90": _mean(covered),
        "stress_nll": _mean(nll_values),
        "stress_direction_accuracy": _direction_accuracy(
            predicted_stress,
            observed_stress,
        ),
        "stress_peak_timing_error_minutes": _peak_timing_error(matched_stress),
        "stress_auc_above_baseline": _stress_auc(results),
        "recovery_half_life_minutes": _recovery_half_life(results),
    }


def aggregate_comparison_metrics(metrics: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if not metrics:
        return {
            "day_count": 0,
            "observation_count": 0,
            "stress_observation_count": 0,
            "stress_mae": None,
            "stress_rmse": None,
            "vitality_mae": None,
            "vitality_rmse": None,
            "cognition_mae": None,
            "cognition_rmse": None,
            "interval_coverage_90": None,
            "stress_nll": None,
            "stress_direction_accuracy": None,
            "stress_peak_timing_error_minutes": None,
            "fatigue_event_count_pileup_correlation": None,
            "fatigue_response_pileup_correlation": None,
        }
    weights = [max(1, int(item.get("stress_observation_count", 0))) for item in metrics]
    return {
        "day_count": len(metrics),
        "observation_count": sum(int(item.get("observation_count", 0)) for item in metrics),
        "stress_observation_count": sum(
            int(item.get("stress_observation_count", 0)) for item in metrics
        ),
        "vitality_observation_count": sum(
            int(item.get("vitality_observation_count", 0)) for item in metrics
        ),
        "cognition_observation_count": sum(
            int(item.get("cognition_observation_count", 0)) for item in metrics
        ),
        "stress_mae": _weighted_present(metrics, "stress_mae", weights),
        "stress_rmse": _weighted_present(metrics, "stress_rmse", weights),
        "vitality_mae": _weighted_present(metrics, "vitality_mae", weights),
        "vitality_rmse": _weighted_present(metrics, "vitality_rmse", weights),
        "cognition_mae": _weighted_present(metrics, "cognition_mae", weights),
        "cognition_rmse": _weighted_present(metrics, "cognition_rmse", weights),
        "interval_coverage_90": _weighted_present(
            metrics, "interval_coverage_90", weights
        ),
        "stress_nll": _weighted_present(metrics, "stress_nll", weights),
        "stress_direction_accuracy": _weighted_present(
            metrics, "stress_direction_accuracy", weights
        ),
        "mean_stress_peak_timing_error_minutes": _weighted_present(
            metrics,
            "stress_peak_timing_error_minutes",
            [1] * len(metrics),
        ),
        "mean_stress_auc_above_baseline": _weighted_present(
            metrics, "stress_auc_above_baseline", [1] * len(metrics)
        ),
        "mean_recovery_half_life_minutes": _weighted_present(
            metrics, "recovery_half_life_minutes", [1] * len(metrics)
        ),
        "mean_fatigue_event_count_pileup_correlation": _weighted_present(
            metrics,
            "fatigue_event_count_pileup_correlation",
            [1] * len(metrics),
        ),
        "mean_fatigue_response_pileup_correlation": _weighted_present(
            metrics,
            "fatigue_response_pileup_correlation",
            [1] * len(metrics),
        ),
    }


def _observable_pile_up_diagnostics(
    results: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    enabled: bool,
    window_minutes: int = 240,
) -> Dict[str, Optional[float]]:
    if not enabled or len(results) < 3:
        return {
            "fatigue_event_count_pileup_correlation": None,
            "fatigue_response_pileup_correlation": None,
        }
    event_starts = []
    for event in events or []:
        if not isinstance(event, Mapping):
            continue
        start = _time_part(event.get("start_time") or event.get("start"))
        if start:
            event_starts.append(time_to_minutes(start))
    minutes = [time_to_minutes(str(row.get("time") or "00:00")) for row in results]
    fatigue = [float(row.get("F", 0.0)) for row in results]
    event_counts = [
        float(sum(current - window_minutes < start <= current for start in event_starts))
        for current in minutes
    ]
    response_load = []
    for index, current in enumerate(minutes):
        total = 0.0
        for prior_index in range(index + 1):
            if current - minutes[prior_index] > window_minutes:
                continue
            row = results[prior_index]
            baseline = float(row.get("stress_baseline", results[0].get("S", 0.0)))
            if prior_index > 0:
                step_minutes = max(1, minutes[prior_index] - minutes[prior_index - 1])
            else:
                step_minutes = 5
            total += (
                max(0.0, float(row.get("S", 0.0)) - baseline)
                * step_minutes
                / 60.0
            )
        response_load.append(total)
    return {
        "fatigue_event_count_pileup_correlation": _correlation(fatigue, event_counts),
        "fatigue_response_pileup_correlation": _correlation(fatigue, response_load),
    }


def evaluate_discrete_baselines(
    train_samples: Sequence[Dict[str, Any]],
    test_samples: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    train = [obs for sample in train_samples for obs in observations_from_sample(sample)]
    test = [obs for sample in test_samples for obs in observations_from_sample(sample)]
    train_s = [obs.stress for obs in train if obs.stress is not None]
    test_s = [obs.stress for obs in test if obs.stress is not None]
    if not test_s:
        return {
            name: {"stress_mae": None, "status": "no_held_out_stress_observations"}
            for name in ("individual_mean", "previous_value", "ar1", "var")
        }
    fallback = _mean(train_s) if train_s else test_s[0]
    mean_predictions = [fallback] * len(test_s)
    previous_predictions = []
    previous = train_s[-1] if train_s else fallback
    for target in test_s:
        previous_predictions.append(previous)
        previous = target
    ar_predictions = _ar1_predictions(train_s, test_s, fallback)
    return {
        "individual_mean": _baseline_metrics(mean_predictions, test_s),
        "previous_value": _baseline_metrics(previous_predictions, test_s),
        "ar1": _baseline_metrics(ar_predictions, test_s),
        "var": _var_baseline(train, test),
    }


def evaluate_event_regression_baseline(
    train_samples: Sequence[Dict[str, Any]],
    test_samples: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """M0-E: transparent event/time regression fitted only on earlier dates."""
    train_rows = _event_regression_rows(train_samples)
    test_rows = _event_regression_rows(test_samples)
    if len(train_rows) < 5 or not test_rows:
        return {
            "stress_mae": None,
            "stress_rmse": None,
            "status": "insufficient_earlier_date_observations",
            "features": _event_regression_feature_names(),
        }
    train_x = np.asarray([row[0] for row in train_rows], dtype=float)
    train_y = np.asarray([row[1] for row in train_rows], dtype=float)
    test_x = np.asarray([row[0] for row in test_rows], dtype=float)
    test_y = [row[1] for row in test_rows]
    # Stabilize correlated inputs without selecting a penalty on held-out dates.
    penalty = np.eye(train_x.shape[1], dtype=float) * 1e-3
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(
        train_x.T @ train_x + penalty,
        train_x.T @ train_y,
    )
    predictions = np.clip(test_x @ coefficients, 0.0, 100.0).tolist()
    return {
        **_baseline_metrics(predictions, test_y),
        "status": "evaluated_on_complete_later_dates",
        "features": _event_regression_feature_names(),
        "uses_subjective_event_appraisal": True,
    }


def _event_regression_rows(
    samples: Sequence[Mapping[str, Any]],
) -> List[tuple[List[float], float]]:
    rows: List[tuple[List[float], float]] = []
    for sample in samples:
        try:
            events = EventFactory.create_from_json(list(sample.get("events", []) or []))
            assessments = build_event_assessments(events)
        except (TypeError, ValueError, KeyError):
            events = []
            assessments = {}
        date = str(sample.get("date") or "")
        for observation in observations_from_sample(sample):
            if observation.stress is None:
                continue
            minute = time_to_minutes(observation.time)
            current_time = datetime.strptime(
                f"{date} {observation.time}",
                "%Y-%m-%d %H:%M",
            )
            inputs = calculate_dynamic_inputs(
                events,
                assessments,
                current_time,
                date,
            )
            angle = 2.0 * math.pi * minute / 1440.0
            rows.append(
                (
                    [
                        1.0,
                        math.sin(angle),
                        math.cos(angle),
                        inputs.event_stress,
                        inputs.task_demand,
                        inputs.recovery,
                        inputs.anticipatory_input,
                        inputs.post_event_input,
                    ],
                    float(observation.stress),
                )
            )
    return rows


def _event_regression_feature_names() -> List[str]:
    return [
        "intercept",
        "time_of_day_sin",
        "time_of_day_cos",
        "appraisal_weighted_event_stress",
        "task_demand",
        "recovery_experience",
        "anticipatory_input",
        "post_event_input",
    ]


def evaluate_hierarchical_m4_readiness(
    samples: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Declare M4 estimability; hierarchy is never fabricated from one user."""
    user_dates: Dict[str, set[str]] = {}
    for sample in samples:
        user_id = str(
            sample.get("user_id")
            or sample.get("participant_id")
            or "single_user_dataset"
        )
        user_dates.setdefault(user_id, set()).add(str(sample.get("date") or ""))
    eligible_users = [
        user_id for user_id, dates in user_dates.items() if len(dates - {""}) >= 7
    ]
    eligible = len(eligible_users) >= 2
    return {
        "base_state_model": "stress-vitality-pc-fatigue-ctssm.m3",
        "layer": "partial_pooling_across_users",
        "individual_parameter_whitelist": [
            "stress_baseline",
            "vitality_baseline",
            "stress_recovery_rate",
            "event_stress_gain",
            "cognition_decay_rate",
        ],
        "user_count": len(user_dates),
        "users_with_at_least_7_dates": len(eligible_users),
        "eligible_for_hierarchical_fit": eligible,
        "fit_performed": False,
        "status": (
            "ready_for_offline_probabilistic_hierarchical_fit"
            if eligible
            else "requires_at_least_two_users_with_sufficient_longitudinal_data"
        ),
        "retention_requires_new_user_holdout_and_posterior_diagnostics": True,
    }


def select_retained_variant(
    reports: Mapping[str, Mapping[str, Any]],
    *,
    test_day_count: int,
    base_params: Mapping[str, Any],
) -> Dict[str, Any]:
    selection_cfg = base_params.get("model_selection", {})
    if not isinstance(selection_cfg, Mapping):
        selection_cfg = {}
    minimum_days = int(selection_cfg.get("minimum_test_days", 7))
    minimum_improvement = float(
        selection_cfg.get("minimum_relative_mae_improvement", 0.03)
    )
    minimum_coverage = float(selection_cfg.get("minimum_interval_coverage", 0.80))
    selected = "m0"
    gates: Dict[str, Dict[str, Any]] = {}
    previous_mae = reports.get("m0", {}).get("test", {}).get("stress_mae")
    for variant in ("m1", "m2", "m3"):
        report = reports.get(variant)
        if not report:
            continue
        current_mae = report.get("test", {}).get("stress_mae")
        coverage = report.get("test", {}).get("interval_coverage_90")
        relative_improvement = None
        if previous_mae is not None and current_mae is not None and previous_mae > 0:
            relative_improvement = (previous_mae - current_mae) / previous_mae
        gate = {
            "complete_date_test_days_sufficient": test_day_count >= minimum_days,
            "out_of_time_error_improved": (
                relative_improvement is not None
                and relative_improvement >= minimum_improvement
            ),
            "interval_coverage_acceptable": (
                coverage is not None and coverage >= minimum_coverage
            ),
            "posterior_not_prior_dominated": None,
            "state_matches_corresponding_ema": _state_has_observations(report, variant),
            "parameter_direction_stable_across_folds": None,
            "no_sampling_divergence_or_nonidentifiability": None,
            "care_frequency_within_predeclared_guardrails": None,
            "complexity_gain_justified": (
                relative_improvement is not None
                and relative_improvement >= minimum_improvement
            ),
            "relative_stress_mae_improvement": relative_improvement,
        }
        gate["retained"] = all(value is True for key, value in gate.items() if key not in {
            "relative_stress_mae_improvement",
            "retained",
        })
        gates[variant] = gate
        if gate["retained"]:
            selected = variant
            previous_mae = current_mae
        else:
            break
    return {
        "active_variant": selected,
        "active_model_version": MODEL_VARIANTS[selected]["canonical"],
        "status": (
            "retained_from_empirical_evidence"
            if selected != "m0"
            else "remain_at_m0_until_all_retention_gates_pass"
        ),
        "test_day_count": test_day_count,
        "retention_gates": gates,
        "automatic_promotion_allowed": selected != "m0",
    }


def _state_has_observations(report: Mapping[str, Any], variant: str) -> Optional[bool]:
    metrics = report.get("test", {})
    if variant == "m1":
        return int(metrics.get("vitality_observation_count", 0)) > 0
    if variant == "m2":
        return int(metrics.get("cognition_observation_count", 0)) > 0
    # F has no direct EMA in the proposed first version.  It must at least move
    # positively with one of the paper-defined observable pile-up diagnostics.
    event_corr = metrics.get("mean_fatigue_event_count_pileup_correlation")
    response_corr = metrics.get("mean_fatigue_response_pileup_correlation")
    available = [
        float(value)
        for value in (event_corr, response_corr)
        if value is not None
    ]
    return max(available) >= 0.20 if available else None


def _ar1_predictions(train: Sequence[float], test: Sequence[float], fallback: float) -> List[float]:
    if len(train) < 3:
        return [fallback] * len(test)
    x = np.asarray(train[:-1], dtype=float)
    y = np.asarray(train[1:], dtype=float)
    design = np.column_stack([np.ones(len(x)), x])
    coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
    previous = float(train[-1])
    predictions = []
    for target in test:
        predicted = float(coefficients[0] + coefficients[1] * previous)
        predictions.append(max(0.0, min(100.0, predicted)))
        previous = float(target)
    return predictions


def _var_baseline(train: Sequence[Observation], test: Sequence[Observation]) -> Dict[str, Any]:
    train_vectors = [
        [obs.stress, obs.vitality, obs.cognition]
        for obs in train
        if obs.stress is not None and obs.vitality is not None and obs.cognition is not None
    ]
    test_vectors = [
        [obs.stress, obs.vitality, obs.cognition]
        for obs in test
        if obs.stress is not None and obs.vitality is not None and obs.cognition is not None
    ]
    if len(train_vectors) < 5 or not test_vectors:
        return {"stress_mae": None, "status": "insufficient_joint_S_V_P_observations"}
    train_array = np.asarray(train_vectors, dtype=float)
    design = np.column_stack([np.ones(len(train_array) - 1), train_array[:-1]])
    targets = train_array[1:]
    coefficients, *_ = np.linalg.lstsq(design, targets, rcond=None)
    previous = train_array[-1]
    predictions = []
    actual = []
    for vector in test_vectors:
        predicted = np.asarray([1.0, *previous]) @ coefficients
        predictions.append(float(max(0.0, min(100.0, predicted[0]))))
        actual.append(float(vector[0]))
        previous = np.asarray(vector, dtype=float)
    return {**_baseline_metrics(predictions, actual), "status": "evaluated"}


def _baseline_metrics(predictions: Sequence[float], targets: Sequence[float]) -> Dict[str, Any]:
    errors = [abs(float(left) - float(right)) for left, right in zip(predictions, targets)]
    squared_errors = [
        (float(left) - float(right)) ** 2
        for left, right in zip(predictions, targets)
    ]
    return {
        "stress_mae": _mean(errors),
        "stress_rmse": (
            math.sqrt(_mean(squared_errors)) if squared_errors else None
        ),
        "observation_count": len(errors),
        "status": "evaluated" if errors else "insufficient_observations",
    }


def _closest_row(results: Sequence[Dict[str, Any]], time: str) -> Optional[Dict[str, Any]]:
    if not results:
        return None
    target = time_to_minutes(time)
    return min(
        results,
        key=lambda row: abs(time_to_minutes(str(row.get("time") or "00:00")) - target),
    )


def _time_part(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    if "T" in text:
        clock = text.split("T", 1)[1]
        if len(clock) >= 5 and clock[2] == ":":
            return clock[:5]
    return ""


def _rating(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    numeric = float(value)
    if 0.0 <= numeric <= 10.0:
        numeric *= 10.0
    return max(0.0, min(100.0, numeric))


def _unit_rating(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    numeric = float(value)
    if numeric > 10.0:
        numeric /= 100.0
    elif numeric > 1.0:
        numeric /= 10.0
    return max(0.0, min(1.0, numeric))


def _direction_accuracy(predicted: Sequence[float], observed: Sequence[float]) -> Optional[float]:
    if len(predicted) < 2 or len(observed) < 2:
        return None
    matches = 0
    total = 0
    for index in range(1, min(len(predicted), len(observed))):
        pred_delta = predicted[index] - predicted[index - 1]
        obs_delta = observed[index] - observed[index - 1]
        pred_bucket = 1 if pred_delta > 3 else -1 if pred_delta < -3 else 0
        obs_bucket = 1 if obs_delta > 3 else -1 if obs_delta < -3 else 0
        matches += int(pred_bucket == obs_bucket)
        total += 1
    return matches / total if total else None


def _peak_timing_error(
    matched: Sequence[tuple[int, float, float]],
) -> Optional[float]:
    if len(matched) < 2:
        return None
    predicted_peak = max(matched, key=lambda item: item[1])[0]
    observed_peak = max(matched, key=lambda item: item[2])[0]
    return float(abs(predicted_peak - observed_peak))


def _stress_auc(results: Sequence[Dict[str, Any]]) -> Optional[float]:
    if len(results) < 2:
        return None
    baseline = float(results[0].get("stress_equilibrium", results[0].get("S", 0.0)))
    total = 0.0
    for left, right in zip(results, results[1:]):
        dt_hours = max(
            0.0,
            (time_to_minutes(str(right.get("time", "00:00")))
             - time_to_minutes(str(left.get("time", "00:00"))))
            / 60.0,
        )
        excess_left = max(0.0, float(left.get("S", 0.0)) - baseline)
        excess_right = max(0.0, float(right.get("S", 0.0)) - baseline)
        total += 0.5 * (excess_left + excess_right) * dt_hours
    return total


def _recovery_half_life(results: Sequence[Dict[str, Any]]) -> Optional[float]:
    if not results:
        return None
    peak_index = max(range(len(results)), key=lambda idx: float(results[idx].get("S", 0.0)))
    peak = float(results[peak_index].get("S", 0.0))
    baseline = float(results[0].get("stress_equilibrium", results[0].get("S", 0.0)))
    if peak <= baseline + 1.0:
        return None
    target = baseline + 0.5 * (peak - baseline)
    start_minutes = time_to_minutes(str(results[peak_index].get("time", "00:00")))
    for row in results[peak_index + 1:]:
        if float(row.get("S", 0.0)) <= target:
            return float(
                time_to_minutes(str(row.get("time", "00:00"))) - start_minutes
            )
    return None


def _weighted_present(
    metrics: Sequence[Dict[str, Any]],
    key: str,
    weights: Sequence[int],
) -> Optional[float]:
    pairs = [
        (float(item[key]), float(weight))
        for item, weight in zip(metrics, weights)
        if item.get(key) is not None
    ]
    if not pairs:
        return None
    total_weight = sum(weight for _, weight in pairs)
    return sum(value * weight for value, weight in pairs) / max(1e-9, total_weight)


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _correlation(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_array = np.asarray(left, dtype=float)
    right_array = np.asarray(right, dtype=float)
    if float(np.std(left_array)) < 1e-9 or float(np.std(right_array)) < 1e-9:
        return None
    return float(np.corrcoef(left_array, right_array)[0, 1])
