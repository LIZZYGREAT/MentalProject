"""Stage-5 hierarchical personalization and bounded residual shadow model.

All training inputs come from an immutable DatasetSnapshot.  The module keeps
the statistical core data-source agnostic so rolling-origin behavior can be
tested without a database, while :class:`ParameterLearningService` owns the
durable candidate/promoted workflow.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from statistics import mean
from typing import Any, Mapping, Sequence
import uuid

import numpy as np
from sqlalchemy import desc, select

from app.db import Database
from app.models import (
    DatasetSnapshot,
    DatasetSnapshotItem,
    LearnedModelProfile,
    ParameterLearningRun,
    Participant,
    ParticipantProfile,
)
from app.repositories import LearnedProfileRepository
from app.services.model_comparison import rolling_origin_splits
from app.services.research_evaluation import ResearchEvaluationService
from app.services.stage4_candidate_replay import Stage4CandidateReplayService


MODEL_FAMILY = "hierarchical-ctssm-residual.v1"
LEARNING_VERSION = "stage5-hierarchical-partial-pooling.v1"
RESIDUAL_VERSION = "stage5-residual-ridge-shadow.v1"
PROMOTION_GATE_VERSION = "stage5-personalization-gate.v1"
MINIMUM_TRAINING_DAYS = 14
MINIMUM_MATCHED_EMA = 30
MINIMUM_WORKLOAD_LEVELS = 3
MINIMUM_RECOVERY_EPISODES = 3
RESIDUAL_MAX = 1.0

PARAMETERS = (
    "S_star_i",
    "workload_sensitivity_i",
    "stress_reactivity_i",
    "stress_recovery_i",
)
DEFAULT_POPULATION = {
    "S_star_i": 5.0,
    "workload_sensitivity_i": 2.8,
    "stress_reactivity_i": 0.7,
    "stress_recovery_i": 0.5,
}
DEFAULT_POPULATION_SD = {
    "S_star_i": 1.25,
    "workload_sensitivity_i": 1.0,
    "stress_reactivity_i": 0.3,
    "stress_recovery_i": 0.25,
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _aware(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _mean_se(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    center = mean(values)
    if len(values) < 2:
        return center, None
    variance = sum((value - center) ** 2 for value in values) / (len(values) - 1)
    return center, math.sqrt(max(0.0, variance) / len(values))


def _workload_level(value: float) -> int:
    return min(4, max(0, int(max(0.0, min(0.999999, value)) * 5)))


def _major_event(sample: Mapping[str, Any]) -> bool:
    context = dict(sample.get("context") or {})
    types = {str(value).lower() for value in context.get("event_types") or []}
    return bool(types - {"rest", "meal", "sleep", "nap", "unknown"}) or bool(
        sample.get("stress_event_since_last")
    )


def _ordered(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for source in samples:
        stress = _number(source.get("actual_stress"))
        workload = _number(source.get("workload"))
        recovery = _number(source.get("recovery"))
        if stress is None or workload is None or recovery is None:
            continue
        try:
            observed_at = _aware(source.get("observed_at"))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                **source,
                "participant_id": str(source.get("participant_id") or ""),
                "local_date": str(source.get("local_date") or observed_at.date()),
                "observed_at": observed_at.isoformat(),
                "actual_stress": max(0.0, min(10.0, stress)),
                "workload": max(0.0, min(1.0, workload)),
                "recovery": max(0.0, min(1.0, recovery)),
            }
        )
    rows.sort(key=lambda row: (row["participant_id"], row["observed_at"]))
    previous: dict[str, dict[str, Any]] = {}
    for row in rows:
        prior = previous.get(row["participant_id"])
        row["previous_stress"] = (
            prior["actual_stress"] if prior is not None else row["actual_stress"]
        )
        row["previous_vitality"] = (
            _number(prior.get("observed_vitality")) if prior is not None else None
        )
        row["hours_since_previous"] = (
            min(
                12.0,
                max(
                    1.0 / 12.0,
                    (_aware(row["observed_at"]) - _aware(prior["observed_at"])).total_seconds()
                    / 3600.0,
                ),
            )
            if prior is not None
            else 1.0
        )
        row["workload_change"] = (
            row["workload"] - prior["workload"] if prior is not None else 0.0
        )
        row["stress_change"] = (
            row["actual_stress"] - prior["actual_stress"] if prior is not None else 0.0
        )
        previous[row["participant_id"]] = row
    return rows


def evidence_counts(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = _ordered(samples)
    recovery = [
        row
        for row in rows
        if row["recovery"] >= 0.25
        and row["stress_change"] < 0
        and row["hours_since_previous"] <= 8
    ]
    reactivity = [
        row
        for row in rows
        if row["workload_change"] >= 0.10
        and row["hours_since_previous"] <= 8
    ]
    baseline = [
        row for row in rows if row["workload"] <= 0.25 and not _major_event(row)
    ]
    levels = sorted({_workload_level(row["workload"]) for row in rows})
    return {
        "observed_days": len({row["local_date"] for row in rows}),
        "matched_ema": len(rows),
        "workload_levels": len(levels),
        "workload_level_bins": levels,
        "baseline_observations": len(baseline),
        "reactivity_transitions": len(reactivity),
        "recovery_episodes": len(recovery),
    }


def minimum_data_gate(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = evidence_counts(samples)
    thresholds = {
        "observed_days": MINIMUM_TRAINING_DAYS,
        "matched_ema": MINIMUM_MATCHED_EMA,
        "workload_levels": MINIMUM_WORKLOAD_LEVELS,
        "recovery_episodes": MINIMUM_RECOVERY_EPISODES,
    }
    checks = {name: counts[name] >= value for name, value in thresholds.items()}
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "counts": counts,
        "thresholds": thresholds,
    }


def _raw_individual_estimate(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows = _ordered(samples)
    baseline_rows = [
        row for row in rows if row["workload"] <= 0.25 and not _major_event(row)
    ]
    baseline_values = [row["actual_stress"] for row in baseline_rows]
    baseline, baseline_se = _mean_se(baseline_values)

    levels = {_workload_level(row["workload"]) for row in rows}
    x = np.asarray([row["workload"] for row in rows], dtype=float)
    y = np.asarray([row["actual_stress"] for row in rows], dtype=float)
    workload_estimate = workload_se = None
    if len(rows) >= 6 and len(levels) >= MINIMUM_WORKLOAD_LEVELS and float(np.var(x)) > 1e-5:
        design = np.column_stack([np.ones(len(x)), x])
        inverse = np.linalg.pinv(design.T @ design)
        coefficient = inverse @ design.T @ y
        residual = y - design @ coefficient
        sigma2 = float(residual @ residual) / max(1, len(y) - 2)
        workload_estimate = max(0.0, min(6.0, float(coefficient[1])))
        workload_se = math.sqrt(max(0.0, sigma2 * float(inverse[1, 1])))

    reactivity_values = []
    recovery_values = []
    for row in rows:
        hours = row["hours_since_previous"]
        if row["workload_change"] >= 0.10 and hours <= 8:
            equilibrium_gap = max(
                0.25,
                (baseline or DEFAULT_POPULATION["S_star_i"])
                + (workload_estimate or DEFAULT_POPULATION["workload_sensitivity_i"])
                * row["workload"]
                - row["previous_stress"],
            )
            rate = max(0.0, row["stress_change"] / hours) / equilibrium_gap
            reactivity_values.append(min(3.0, rate))
        if row["recovery"] >= 0.25 and row["stress_change"] < 0 and hours <= 8:
            gap = max(
                0.25,
                row["previous_stress"] - (baseline or DEFAULT_POPULATION["S_star_i"]),
            )
            rate = max(0.0, -row["stress_change"] / hours) / (gap * row["recovery"])
            recovery_values.append(min(3.0, rate))
    reactivity, reactivity_se = _mean_se(reactivity_values)
    recovery, recovery_se = _mean_se(recovery_values)
    return {
        "S_star_i": {
            "estimate": baseline,
            "std_error": baseline_se,
            "sample_count": len(baseline_values),
            "identified": len(baseline_values) >= 3,
            "observation": "low_workload_no_major_event_ema_stress",
        },
        "workload_sensitivity_i": {
            "estimate": workload_estimate,
            "std_error": workload_se,
            "sample_count": len(rows),
            "identified": workload_estimate is not None,
            "observation": "workload_level_contrast_to_ema_stress",
        },
        "stress_reactivity_i": {
            "estimate": reactivity,
            "std_error": reactivity_se,
            "sample_count": len(reactivity_values),
            "identified": len(reactivity_values) >= 3 and reactivity is not None,
            "observation": "stress_rise_after_workload_increase",
        },
        "stress_recovery_i": {
            "estimate": recovery,
            "std_error": recovery_se,
            "sample_count": len(recovery_values),
            "identified": len(recovery_values) >= MINIMUM_RECOVERY_EPISODES and recovery is not None,
            "observation": "stress_decline_after_recovery_start",
        },
    }


def estimate_population_prior(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float]]:
    by_participant: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in samples:
        by_participant[str(row.get("participant_id") or "")].append(row)
    raw = [_raw_individual_estimate(rows) for rows in by_participant.values()]
    result = {}
    for name in PARAMETERS:
        estimates = [
            float(value[name]["estimate"])
            for value in raw
            if value[name]["identified"] and value[name]["estimate"] is not None
        ]
        center = mean(estimates) if estimates else DEFAULT_POPULATION[name]
        between = (
            math.sqrt(
                sum((value - center) ** 2 for value in estimates)
                / (len(estimates) - 1)
            )
            if len(estimates) >= 2
            else DEFAULT_POPULATION_SD[name]
        )
        result[name] = {
            "mean": round(center, 6),
            "std_dev": round(max(0.05, between), 6),
            "participant_count": len(estimates),
        }
    return result


def fit_partial_pooling(
    population_samples: Sequence[Mapping[str, Any]],
    individual_samples: Sequence[Mapping[str, Any]],
    *,
    trait_resilience: float | None = None,
) -> dict[str, Any]:
    population = estimate_population_prior(population_samples)
    raw = _raw_individual_estimate(individual_samples)
    values: dict[str, float] = {}
    uncertainty: dict[str, dict[str, Any]] = {}
    for name in PARAMETERS:
        prior_mean = float(population[name]["mean"])
        if name == "stress_recovery_i" and trait_resilience is not None:
            prior_mean += 0.25 * max(0.0, min(1.0, trait_resilience))
        prior_variance = float(population[name]["std_dev"]) ** 2
        evidence = raw[name]
        individual = _number(evidence.get("estimate"))
        standard_error = _number(evidence.get("std_error"))
        if not evidence["identified"] or individual is None or standard_error is None:
            weight = 0.0
            estimate = prior_mean
            posterior_se = math.sqrt(prior_variance)
            status = "population_prior_insufficient_contrast"
        else:
            sampling_variance = max(1e-6, standard_error**2)
            weight = prior_variance / (prior_variance + sampling_variance)
            estimate = weight * individual + (1.0 - weight) * prior_mean
            posterior_se = math.sqrt(
                prior_variance * sampling_variance
                / (prior_variance + sampling_variance)
            )
            status = "partially_pooled"
        if name in {"stress_reactivity_i", "stress_recovery_i", "workload_sensitivity_i"}:
            estimate = max(0.0, estimate)
        if name == "S_star_i":
            estimate = max(0.0, min(10.0, estimate))
        values[name] = round(estimate, 6)
        interval_width = 2.0 * 1.96 * posterior_se
        wide_threshold = (
            2.0
            if name in {"S_star_i", "workload_sensitivity_i"}
            else 0.75
        )
        uncertainty[name] = {
            "estimate": round(estimate, 6),
            "std_error": round(posterior_se, 6),
            "interval_95": {
                "lower": round(estimate - 1.96 * posterior_se, 6),
                "upper": round(estimate + 1.96 * posterior_se, 6),
            },
            "sample_count": int(evidence["sample_count"]),
            "pooling_weight": round(weight, 6),
            "evidence_status": status,
            "interval_width": round(interval_width, 6),
            "evidence_strength": (
                "insufficient_evidence_wide_interval"
                if interval_width > wide_threshold
                else "supported"
            ),
            "observation": evidence["observation"],
        }
    return {
        "parameters": values,
        "uncertainty": uncertainty,
        "population_prior": population,
        "individual_raw": raw,
        "trait_resilience": trait_resilience,
        "method": LEARNING_VERSION,
    }


def _normalize_runtime_parameters(source: Mapping[str, Any] | None) -> dict[str, float]:
    value = dict(source or {})
    hierarchical = dict(value.get("hierarchical_parameters") or {})
    ctssm = dict(value.get("ctssm_params") or {})
    baseline = _number(hierarchical.get("S_star_i"))
    if baseline is None:
        stored = _number(value.get("S_star_init"))
        baseline = stored / 10.0 if stored is not None and stored > 10 else stored
    return {
        "S_star_i": baseline if baseline is not None else DEFAULT_POPULATION["S_star_i"],
        "workload_sensitivity_i": _number(hierarchical.get("workload_sensitivity_i"))
        or ((_number(ctssm.get("workload_stress_gain")) or 28.0) / 10.0),
        "stress_reactivity_i": _number(hierarchical.get("stress_reactivity_i"))
        or (_number(ctssm.get("stress_reactivity_per_hour")) or DEFAULT_POPULATION["stress_reactivity_i"]),
        "stress_recovery_i": _number(hierarchical.get("stress_recovery_i"))
        or (_number(ctssm.get("stress_recovery_per_hour")) or DEFAULT_POPULATION["stress_recovery_i"]),
    }


def runtime_candidate_parameters(fitted: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(fitted["parameters"])
    return {
        "hierarchical_parameters": value,
        "S_star_init": round(value["S_star_i"] * 10.0, 6),
        "ctssm_params": {
            "workload_stress_gain": round(value["workload_sensitivity_i"] * 10.0, 6),
            "stress_reactivity_per_hour": value["stress_reactivity_i"],
            "stress_recovery_per_hour": value["stress_recovery_i"],
            "recovery_stress_gain": round(value["stress_recovery_i"] * 10.0, 6),
            "trait_resilience_prior": fitted.get("trait_resilience"),
        },
        "hierarchical_population_prior": dict(fitted["population_prior"]),
    }


def _predict_base(
    row: Mapping[str, Any], parameters: Mapping[str, float]
) -> float:
    previous = _number(row.get("previous_stress"))
    if previous is None:
        previous = parameters["S_star_i"]
    equilibrium = parameters["S_star_i"] + parameters["workload_sensitivity_i"] * float(row["workload"])
    rising = equilibrium >= previous
    rate = parameters["stress_reactivity_i"] if rising else parameters["stress_recovery_i"]
    if not rising:
        rate *= 1.0 + float(row.get("recovery") or 0.0)
    hours = max(1.0 / 12.0, min(12.0, float(row.get("hours_since_previous") or 1.0)))
    predicted = previous + (equilibrium - previous) * (1.0 - math.exp(-max(0.0, rate) * hours))
    return max(0.0, min(10.0, predicted))


def _residual_feature_map(row: Mapping[str, Any]) -> dict[str, float]:
    observed = _aware(row["observed_at"])
    context = dict(row.get("context") or {})
    minute = observed.hour * 60 + observed.minute
    angle = 2.0 * math.pi * minute / 1440.0
    values = {
        "hour_sin": math.sin(angle),
        "hour_cos": math.cos(angle),
        f"weekday:{observed.weekday()}": 1.0,
        "workload": float(row.get("workload") or 0.0),
        "continuous_load": float(row.get("continuous_load") or 0.0),
        "previous_stress": float(row.get("previous_stress") or 0.0) / 10.0,
        "previous_vitality": float(row.get("previous_vitality") or 0.0) / 10.0,
        "recovery_window": float(row.get("recovery") or 0.0),
    }
    for kind in context.get("event_types") or []:
        values[f"event_type:{str(kind)[:48]}"] = 1.0
    for course in context.get("courses") or []:
        values[f"course:{str(course)[:64]}"] = 1.0
    semantic = context.get("semantic_dimensions") or row.get("semantic_dimensions") or {}
    if isinstance(semantic, Mapping):
        for name, raw in semantic.items():
            number = _number(raw)
            if number is not None:
                values[f"semantic:{str(name)[:48]}"] = number
    return values


def fit_residual_ridge(
    samples: Sequence[Mapping[str, Any]],
    base_parameters: Mapping[str, float],
    *,
    ridge: float = 2.0,
    residual_max: float = RESIDUAL_MAX,
) -> dict[str, Any]:
    rows = _ordered(samples)
    feature_maps = [_residual_feature_map(row) for row in rows]
    names = sorted({name for values in feature_maps for name in values})
    if not rows or not names:
        return {
            "version": RESIDUAL_VERSION,
            "mode": "shadow",
            "feature_names": [],
            "coefficients": [],
            "intercept": 0.0,
            "residual_max": residual_max,
            "sample_count": len(rows),
            "residual_sd": None,
        }
    design = np.asarray(
        [[values.get(name, 0.0) for name in names] for values in feature_maps],
        dtype=float,
    )
    target = np.asarray(
        [row["actual_stress"] - _predict_base(row, base_parameters) for row in rows],
        dtype=float,
    )
    augmented = np.column_stack([np.ones(len(rows)), design])
    penalty = np.eye(augmented.shape[1]) * ridge
    penalty[0, 0] = 0.0
    coefficient = np.linalg.pinv(augmented.T @ augmented + penalty) @ augmented.T @ target
    errors = target - augmented @ coefficient
    return {
        "version": RESIDUAL_VERSION,
        "mode": "shadow",
        "feature_names": names,
        "coefficients": [round(float(value), 8) for value in coefficient[1:]],
        "intercept": round(float(coefficient[0]), 8),
        "ridge_lambda": ridge,
        "residual_max": residual_max,
        "sample_count": len(rows),
        "residual_sd": round(float(np.sqrt(np.mean(errors**2))), 6),
    }


def predict_residual(row: Mapping[str, Any], model: Mapping[str, Any]) -> float:
    values = _residual_feature_map(row)
    estimate = float(model.get("intercept") or 0.0) + sum(
        float(coefficient) * values.get(name, 0.0)
        for name, coefficient in zip(
            model.get("feature_names") or [], model.get("coefficients") or []
        )
    )
    bound = max(0.0, float(model.get("residual_max") or RESIDUAL_MAX))
    return max(-bound, min(bound, estimate))


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "mae": None,
            "rmse": None,
            "coverage": None,
            "peak_timing_error_minutes": None,
            "sample_count": 0,
        }
    errors = [float(row["predicted"]) - float(row["actual_stress"]) for row in rows]
    coverage_rows = [
        row for row in rows if row.get("lower") is not None and row.get("upper") is not None
    ]
    actual_peaks: dict[tuple[str, str], Mapping[str, Any]] = {}
    predicted_peaks: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (str(row.get("participant_id")), str(row.get("local_date")))
        if key not in actual_peaks or row["actual_stress"] > actual_peaks[key]["actual_stress"]:
            actual_peaks[key] = row
        if key not in predicted_peaks or row["predicted"] > predicted_peaks[key]["predicted"]:
            predicted_peaks[key] = row
    timing = [
        abs((_aware(actual_peaks[key]["observed_at"]) - _aware(predicted_peaks[key]["observed_at"])).total_seconds()) / 60.0
        for key in actual_peaks
    ]
    return {
        "mae": round(mean(abs(value) for value in errors), 6),
        "rmse": round(math.sqrt(mean(value * value for value in errors)), 6),
        "coverage": (
            round(
                mean(
                    float(row["lower"]) <= float(row["actual_stress"]) <= float(row["upper"])
                    for row in coverage_rows
                ),
                6,
            )
            if coverage_rows
            else None
        ),
        "peak_timing_error_minutes": round(mean(timing), 3) if timing else None,
        "sample_count": len(rows),
    }


def _gate_non_decrease(candidate: Any, baseline: Any) -> bool:
    return candidate is not None and baseline is not None and float(candidate) >= float(baseline)


def _gate_non_increase(candidate: Any, baseline: Any) -> bool:
    return candidate is not None and baseline is not None and float(candidate) <= float(baseline)


def rolling_personalization_validation(
    population_samples: Sequence[Mapping[str, Any]],
    individual_samples: Sequence[Mapping[str, Any]],
    *,
    explicit_parameters: Mapping[str, Any] | None = None,
    current_parameters: Mapping[str, Any] | None = None,
    trait_resilience: float | None = None,
) -> dict[str, Any]:
    population_rows = _ordered(population_samples)
    rows = _ordered(individual_samples)
    splits = rolling_origin_splits(rows, minimum_training_days=MINIMUM_TRAINING_DAYS)
    predictions: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "global_model",
            "explicit_profile_model",
            "current_personalized_model",
            "new_candidate_model",
            "candidate_with_residual_shadow",
        )
    }
    split_metrics = []
    for split in splits:
        origin_cutoff = datetime.combine(
            date.fromisoformat(split["test_days"][0]), time.min, timezone.utc
        )
        training = [
            rows[index]
            for index in split["train_indices"]
            if not rows[index].get("observation_created_at")
            or _aware(rows[index]["observation_created_at"]) < origin_cutoff
        ]
        testing = [rows[index] for index in split["test_indices"]]
        cutoff_day = split["test_days"][0]
        population_training = [
            row for row in population_rows if str(row["local_date"]) < cutoff_day
        ]
        if not population_training:
            population_training = training
        fitted = fit_partial_pooling(
            population_training, training, trait_resilience=trait_resilience
        )
        candidate = dict(fitted["parameters"])
        global_parameters = {
            name: float(fitted["population_prior"][name]["mean"])
            for name in PARAMETERS
        }
        explicit = _normalize_runtime_parameters(explicit_parameters)
        current = _normalize_runtime_parameters(current_parameters)
        residual = fit_residual_ridge(training, candidate)
        residual_sd = float(residual.get("residual_sd") or 1.0)
        width = max(0.35, 1.645 * residual_sd)
        split_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in testing:
            base_predictions = {
                "global_model": _predict_base(row, global_parameters),
                "explicit_profile_model": _predict_base(row, explicit),
                "current_personalized_model": _predict_base(row, current),
                "new_candidate_model": _predict_base(row, candidate),
            }
            base_predictions["candidate_with_residual_shadow"] = max(
                0.0,
                min(
                    10.0,
                    base_predictions["new_candidate_model"]
                    + predict_residual(row, residual),
                ),
            )
            for family, predicted in base_predictions.items():
                result = {
                    **row,
                    "predicted": predicted,
                    "lower": max(0.0, predicted - width),
                    "upper": min(10.0, predicted + width),
                    "split_index": split["split_index"],
                }
                predictions[family].append(result)
                split_rows[family].append(result)
        split_metrics.append(
            {
                "split_index": split["split_index"],
                "training_days": split["training_days"],
                "test_days": split["test_days"],
                "training_sample_count": len(training),
                "metrics": {name: _metrics(value) for name, value in split_rows.items()},
            }
        )
    metrics = {name: _metrics(value) for name, value in predictions.items()}
    candidate = metrics["new_candidate_model"]
    comparators = {
        name: metrics[name]
        for name in (
            "global_model",
            "explicit_profile_model",
            "current_personalized_model",
        )
    }
    stable_wins = {
        name: sum(
            1
            for split in split_metrics
            if split["metrics"]["new_candidate_model"]["mae"]
            < split["metrics"][name]["mae"]
        )
        for name in comparators
    }
    required_wins = max(1, math.ceil(len(split_metrics) * 0.6)) if split_metrics else 1
    promotion_checks = {
        "rolling_origin_available": bool(split_metrics),
        "mae_better_than_global": bool(split_metrics) and candidate["mae"] < comparators["global_model"]["mae"],
        "mae_better_than_explicit": bool(split_metrics) and candidate["mae"] < comparators["explicit_profile_model"]["mae"],
        "mae_better_than_current_personalized": bool(split_metrics) and candidate["mae"] < comparators["current_personalized_model"]["mae"],
        "stable_split_improvement": bool(split_metrics) and all(value >= required_wins for value in stable_wins.values()),
        "coverage_non_inferior": _gate_non_decrease(candidate["coverage"], comparators["current_personalized_model"]["coverage"]),
        "peak_timing_non_inferior": _gate_non_increase(candidate["peak_timing_error_minutes"], comparators["current_personalized_model"]["peak_timing_error_minutes"]),
    }
    residual_metrics = metrics["candidate_with_residual_shadow"]
    residual_checks = {
        "oot_mae_improved": residual_metrics["mae"] is not None and candidate["mae"] is not None and residual_metrics["mae"] < candidate["mae"],
        "coverage_not_decreased": _gate_non_decrease(residual_metrics["coverage"], candidate["coverage"]),
        "peak_error_not_worse": _gate_non_increase(residual_metrics["peak_timing_error_minutes"], candidate["peak_timing_error_minutes"]),
        "correction_bound": RESIDUAL_MAX,
        "mode": "shadow",
    }
    return {
        "rolling_origin": {
            "version": "stage5-expanding-day-origin.v1",
            "minimum_training_days": MINIMUM_TRAINING_DAYS,
            "split_count": len(split_metrics),
            "splits": split_metrics,
        },
        "comparison": metrics,
        "promotion_gate": {
            "version": PROMOTION_GATE_VERSION,
            "passed": all(promotion_checks.values()),
            "checks": promotion_checks,
            "stable_wins": stable_wins,
            "required_wins": required_wins,
        },
        "residual_gate": {
            "version": "stage5-residual-gate.v1",
            "passed": all(
                residual_checks[name]
                for name in (
                    "oot_mae_improved",
                    "coverage_not_decreased",
                    "peak_error_not_worse",
                )
            ),
            "checks": residual_checks,
            "display": {
                "ctssm_baseline": candidate,
                "residual_correction": residual_metrics,
                "final_prediction": "shadow_only_not_applied_to_active_runtime",
            },
        },
    }


def _trait_resilience(items: Sequence[Mapping[str, Any]], participant_id: uuid.UUID) -> float | None:
    candidates = []
    for item in items:
        if item["item_type"] != "psychometric" or item["participant_id"] != participant_id:
            continue
        metadata = dict(item["metadata"])
        if str(metadata.get("instrument_name") or "").upper() != "BRS":
            continue
        scores = dict(metadata.get("scores") or {})
        score = next(
            (_number(scores.get(name)) for name in ("mean", "total_mean", "score", "brs_mean") if scores.get(name) is not None),
            None,
        )
        if score is None and _number(scores.get("total")) is not None:
            score = float(scores["total"]) / 6.0
        if score is not None and 1.0 <= score <= 5.0:
            candidates.append((str(metadata.get("administered_at") or ""), (score - 1.0) / 4.0))
    return max(candidates)[1] if candidates else None


def _uncertainty_payload(fitted: Mapping[str, Any], residual: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(fitted["uncertainty"])

    def scaled(name: str, factor: float = 1.0) -> dict[str, Any]:
        value = dict(source[name])
        result = dict(value)
        result["estimate"] = round(float(value["estimate"]) * factor, 6)
        result["std_error"] = round(float(value["std_error"]) * factor, 6)
        result["interval_95"] = {
            key: round(float(number) * factor, 6)
            for key, number in dict(value["interval_95"]).items()
        }
        return result

    return {
        "hierarchical_parameters": source,
        "S_star_init": scaled("S_star_i", 10.0),
        "ctssm_params": {
            "workload_stress_gain": scaled("workload_sensitivity_i", 10.0),
            "stress_reactivity_per_hour": scaled("stress_reactivity_i"),
            "stress_recovery_per_hour": scaled("stress_recovery_i"),
            "recovery_stress_gain": scaled("stress_recovery_i", 10.0),
        },
        "hierarchical_population_prior": {
            "std_error": mean(float(value["std_dev"]) for value in fitted["population_prior"].values())
        },
        "residual_model": {
            "std_error": float(residual.get("residual_sd") or 0.0),
            "sample_count": int(residual.get("sample_count") or 0),
        },
    }


class ParameterLearningService:
    """Create, reject, promote, list and weekly-schedule Stage-5 runs."""

    SNAPSHOT_WINDOW_DAYS = 56

    def __init__(self, database: Database, timezone_name: str):
        self.database = database
        self.timezone_name = timezone_name
        self.learned_profiles = LearnedProfileRepository(database)
        self.research = ResearchEvaluationService(database, timezone_name)
        self.extractor = Stage4CandidateReplayService(timezone_name)

    @staticmethod
    def _view(row: ParameterLearningRun) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "dataset_snapshot_id": str(row.dataset_snapshot_id),
            "model_family": row.model_family,
            "parameters_before": dict(row.parameters_before or {}),
            "parameters_candidate": dict(row.parameters_candidate or {}),
            "training_metrics": dict(row.training_metrics or {}),
            "validation_metrics": dict(row.validation_metrics or {}),
            "sample_count": row.sample_count,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
        }

    def list_runs(
        self, *, participant_id: uuid.UUID | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            statement = select(ParameterLearningRun)
            if participant_id is not None:
                statement = statement.where(ParameterLearningRun.participant_id == participant_id)
            rows = session.execute(
                statement.order_by(desc(ParameterLearningRun.created_at)).limit(max(1, min(limit, 500)))
            ).scalars().all()
            return [self._view(row) for row in rows]

    def _snapshot(self, snapshot_id: uuid.UUID) -> tuple[DatasetSnapshot, list[dict[str, Any]]]:
        with self.database.session() as session:
            snapshot = session.get(DatasetSnapshot, snapshot_id)
            if snapshot is None:
                raise ValueError("dataset snapshot not found")
            rows = session.execute(
                select(DatasetSnapshotItem).where(DatasetSnapshotItem.dataset_snapshot_id == snapshot_id)
            ).scalars().all()
            items = [
                {
                    "item_type": row.item_type,
                    "source_id": row.source_id,
                    "source_version": row.source_version,
                    "participant_id": row.participant_id,
                    "local_date": row.local_date,
                    "source_hash": row.source_hash,
                    "metadata": dict(row.metadata_json or {}),
                }
                for row in rows
            ]
            return snapshot, items

    def _explicit_as_of(self, participant_id: uuid.UUID, cutoff: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(ParticipantProfile)
                .where(
                    ParticipantProfile.participant_id == participant_id,
                    ParticipantProfile.created_at <= cutoff,
                )
                .order_by(desc(ParticipantProfile.version))
                .limit(1)
            ).scalar_one_or_none()
            payload = dict(row.profile_json or {}) if row else {}
            parameters = dict(payload.get("model_params") or payload.get("params") or {})
            provenance = {
                "version": row.version if row else None,
                "created_at": row.created_at.isoformat() if row else None,
                "sha256": hashlib.sha256(
                    json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            return parameters, provenance

    def train_snapshot(
        self, snapshot_id: uuid.UUID, participant_id: uuid.UUID
    ) -> dict[str, Any]:
        snapshot, items = self._snapshot(snapshot_id)
        member_ids = {item["participant_id"] for item in items if item["item_type"] == "participant"}
        if participant_id not in member_ids:
            raise ValueError("participant is not a member of the dataset snapshot")
        extracted_population = self.extractor._extract(items, None)["samples"]
        individual = [row for row in extracted_population if str(row.get("participant_id")) == str(participant_id)]
        gate = minimum_data_gate(individual)
        resilience = _trait_resilience(items, participant_id)
        fitted = fit_partial_pooling(
            extracted_population or individual,
            individual,
            trait_resilience=resilience,
        )
        current = self.learned_profiles.runtime_active(participant_id)
        before = dict((current or {}).get("parameters") or {})
        explicit, explicit_provenance = self._explicit_as_of(participant_id, snapshot.observation_cutoff)
        validation = rolling_personalization_validation(
            extracted_population or individual,
            individual,
            explicit_parameters=explicit,
            current_parameters=before,
            trait_resilience=resilience,
        )
        residual = fit_residual_ridge(individual, fitted["parameters"])
        candidate = runtime_candidate_parameters(fitted)
        candidate["residual_model"] = residual
        uncertainty = _uncertainty_payload(fitted, residual)
        status = (
            "candidate"
            if gate["passed"] and validation["promotion_gate"]["passed"]
            else "rejected"
        )
        run_id = uuid.uuid4()
        training_metrics = {
            "learning_version": LEARNING_VERSION,
            "minimum_data_gate": gate,
            "population_prior": fitted["population_prior"],
            "parameter_evidence": fitted["uncertainty"],
            "trait_resilience": resilience,
            "explicit_profile_provenance": explicit_provenance,
            "schedule": "weekly_dataset_snapshot",
        }
        validation_metrics = {
            **validation,
            "uncertainty": uncertainty,
            "candidate_profile_status": status,
        }
        model_selection = {
            **dict(before.get("model_selection") or {}),
            "workflow": "stage5_candidate_active",
            "status": f"stage5_{status}",
            "parameter_learning_run_id": str(run_id),
            "dataset_snapshot_id": str(snapshot_id),
            "promotion_gate_version": PROMOTION_GATE_VERSION,
            "residual_mode": "shadow",
        }
        profile_parameters = {**candidate, "model_selection": model_selection}
        window_start = min((date.fromisoformat(str(row["local_date"])) for row in individual), default=snapshot.date_start)
        window_end = max((date.fromisoformat(str(row["local_date"])) for row in individual), default=snapshot.date_end)
        with self.database.session() as session:
            session.add(
                ParameterLearningRun(
                    id=run_id,
                    participant_id=participant_id,
                    dataset_snapshot_id=snapshot_id,
                    model_family=MODEL_FAMILY,
                    parameters_before=before,
                    parameters_candidate=candidate,
                    training_metrics=training_metrics,
                    validation_metrics=validation_metrics,
                    sample_count=len(individual),
                    status=status,
                )
            )
            self.learned_profiles.save_in_session(
                session,
                participant_id,
                parameters=profile_parameters,
                uncertainty=uncertainty,
                sample_count=len(individual),
                day_count=gate["counts"]["observed_days"],
                confidence=min(0.95, len(individual) / 60.0),
                window_start=window_start,
                window_end=window_end,
                source=LEARNING_VERSION,
                model_version="mindflow-ctssm-runtime-v9",
                validation_status="candidate" if status == "candidate" else "rejected",
            )
        with self.database.session() as session:
            return self._view(session.get(ParameterLearningRun, run_id))

    def promote(self, run_id: uuid.UUID) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(ParameterLearningRun, run_id)
            if run is None:
                raise ValueError("parameter learning run not found")
            if run.status != "candidate":
                raise ValueError("only a candidate parameter learning run can be promoted")
            validation = dict(run.validation_metrics or {})
            if not bool((validation.get("promotion_gate") or {}).get("passed")):
                raise ValueError("candidate did not pass personalized promotion gate")
            if not bool((run.training_metrics.get("minimum_data_gate") or {}).get("passed")):
                raise ValueError("candidate did not pass minimum data gate")
            candidate = dict(run.parameters_candidate or {})
            uncertainty = dict(validation.get("uncertainty") or {})
            candidate_profiles = session.execute(
                select(LearnedModelProfile)
                .where(
                    LearnedModelProfile.participant_id == run.participant_id,
                    LearnedModelProfile.source == LEARNING_VERSION,
                    LearnedModelProfile.validation_status == "candidate",
                )
                .order_by(desc(LearnedModelProfile.version))
            ).scalars().all()
            candidate_profile = next(
                (
                    profile
                    for profile in candidate_profiles
                    if str(
                        (profile.parameters_json.get("model_selection") or {}).get(
                            "parameter_learning_run_id"
                        )
                    )
                    == str(run.id)
                ),
                None,
            )
            if candidate_profile is None:
                raise ValueError("candidate learned profile is missing")
            selection = {
                **dict((candidate_profile.parameters_json.get("model_selection") or {})),
                "status": "stage5_promoted",
                "promoted_at": datetime.now(timezone.utc).isoformat(),
                "residual_mode": "shadow",
            }
            promoted_parameters = {**candidate, "model_selection": selection}
            active = self.learned_profiles.save_in_session(
                session,
                run.participant_id,
                parameters=promoted_parameters,
                uncertainty=uncertainty,
                sample_count=run.sample_count,
                day_count=int((run.training_metrics["minimum_data_gate"]["counts"])["observed_days"]),
                confidence=min(0.95, run.sample_count / 60.0),
                window_start=candidate_profile.window_start,
                window_end=candidate_profile.window_end,
                source=f"{LEARNING_VERSION}.promoted",
                model_version="mindflow-ctssm-runtime-v9",
                validation_status="validated",
            )
            run.status = "promoted"
            validation["promotion_history"] = {
                "before": dict(run.parameters_before or {}),
                "after": candidate,
                "metrics": dict(validation.get("comparison") or {}),
                "reason": "minimum_data_and_rolling_origin_gate_passed",
                "promoted_at": selection["promoted_at"],
            }
            run.validation_metrics = validation
            session.flush()
            result = self._view(run)
            result["active_profile"] = active
            return result

    def maybe_calibrate(self, participant_id: uuid.UUID, *, through: date) -> dict[str, Any]:
        start_of_week = through - timedelta(days=through.weekday())
        with self.database.session() as session:
            latest = session.execute(
                select(ParameterLearningRun)
                .where(ParameterLearningRun.participant_id == participant_id)
                .order_by(desc(ParameterLearningRun.created_at))
                .limit(1)
            ).scalar_one_or_none()
            if latest is not None and latest.created_at.date() >= start_of_week:
                return {"status": "weekly_run_already_exists", "run": self._view(latest)}
            participant = session.get(Participant, participant_id)
            if participant is None:
                raise ValueError("participant not found")
        date_start = through - timedelta(days=self.SNAPSHOT_WINDOW_DAYS - 1)
        # One cohort snapshot supplies the actual population prior. Reuse the
        # same weekly boundary for later participants instead of silently
        # estimating a "population" from one participant.
        with self.database.session() as session:
            existing_snapshot = session.execute(
                select(DatasetSnapshot)
                .where(
                    DatasetSnapshot.date_start == date_start,
                    DatasetSnapshot.date_end == through,
                )
                .order_by(desc(DatasetSnapshot.created_at))
            ).scalars().first()
            if existing_snapshot is not None and dict(
                existing_snapshot.participant_filter or {}
            ) != {"participant_codes": []}:
                existing_snapshot = None
            existing_snapshot_id = (
                str(existing_snapshot.id) if existing_snapshot is not None else None
            )
        snapshot = (
            {"id": existing_snapshot_id}
            if existing_snapshot_id is not None
            else self.research.create_dataset_snapshot(
                date_start=date_start,
                date_end=through,
                participant_filter={},
            )
        )
        run = self.train_snapshot(uuid.UUID(snapshot["id"]), participant_id)
        return {"status": run["status"], "snapshot": snapshot, "run": run}

    def admin_dashboard(self, *, participant_id: uuid.UUID | None = None) -> dict[str, Any]:
        runs = self.list_runs(participant_id=participant_id, limit=200)
        return {
            "calibration_runs": runs,
            "parameter_history": [
                {
                    "run_id": row["id"],
                    "created_at": row["created_at"],
                    "status": row["status"],
                    "before": row["parameters_before"],
                    "candidate": row["parameters_candidate"],
                    "uncertainty": row["validation_metrics"].get("uncertainty"),
                }
                for row in runs
            ],
            "candidate_vs_active": [
                {
                    "run_id": row["id"],
                    "candidate": row["parameters_candidate"],
                    "active_before": row["parameters_before"],
                    "status": row["status"],
                }
                for row in runs
            ],
            "residual_diagnostics": [
                {
                    "run_id": row["id"],
                    "model": row["parameters_candidate"].get("residual_model"),
                    "gate": row["validation_metrics"].get("residual_gate"),
                }
                for row in runs
            ],
            "promotion_history": [
                row["validation_metrics"]["promotion_history"]
                for row in runs
                if row["validation_metrics"].get("promotion_history")
            ],
        }
