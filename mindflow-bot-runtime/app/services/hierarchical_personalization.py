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
import math
from statistics import mean
from typing import Any, Mapping, Sequence
import uuid

import numpy as np
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from algorithm.dynamic_state_model import normalize_model_variant

from app.db import Database
from app.models import (
    DatasetSnapshot,
    DatasetSnapshotItem,
    LearnedModelProfile,
    ParameterLearningRun,
    Participant,
)
from app.repositories import LearnedProfileRepository, promotion_parameters_hash
from app.services.dataset_snapshot_integrity import DatasetSnapshotIntegrityService
from app.services.model_comparison import comparison_metrics, rolling_origin_splits
from app.services.research_evaluation import (
    DATASET_SCHEMA_V7,
    ResearchEvaluationService,
    STAGE5_INTERVENTION_EXCLUSION_MINUTES,
)
from app.services.stage4_candidate_replay import (
    REPLAY_ENGINE_VERSION,
    Stage4CandidateReplayService,
)


MODEL_FAMILY = "hierarchical-ctssm-residual.v2"
LEARNING_VERSION = "stage5-hierarchical-partial-pooling.v3"
RESIDUAL_VERSION = "stage5-residual-ridge-shadow.v2"
PROMOTION_GATE_VERSION = "stage5-personalization-gate.v3"
MINIMUM_TRAINING_DAYS = 14
MINIMUM_MATCHED_EMA = 30
MINIMUM_WORKLOAD_LEVELS = 3
MINIMUM_RECOVERY_EPISODES = 3
RESIDUAL_MAX = 1.0

PARAMETERS = (
    "S_star_i",
    "workload_sensitivity_i",
    "recovery_sensitivity_i",
    "stress_reactivity_i",
    "stress_recovery_rate_i",
)
DEFAULT_POPULATION = {
    "S_star_i": 5.0,
    "workload_sensitivity_i": 2.8,
    "recovery_sensitivity_i": 2.0,
    "stress_reactivity_i": 0.7,
    "stress_recovery_rate_i": 0.5,
}
DEFAULT_POPULATION_SD = {
    "S_star_i": 1.25,
    "workload_sensitivity_i": 1.0,
    "recovery_sensitivity_i": 0.8,
    "stress_reactivity_i": 0.3,
    "stress_recovery_rate_i": 0.25,
}
BRS_PRIOR_VERSION = "stage5-brs-separated-prior.v1"
BRS_RECOVERY_SENSITIVITY_SHIFT = 0.40
BRS_RECOVERY_RATE_SHIFT = 0.10


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
        created_at = observed_at
        for key in ("observation_created_at", "created_at"):
            if source.get(key) is not None:
                try:
                    created_at = _aware(source.get(key))
                    break
                except (TypeError, ValueError):
                    pass
        rows.append(
            {
                **source,
                "participant_id": str(source.get("participant_id") or ""),
                "local_date": str(source.get("local_date") or observed_at.date()),
                "observed_at": observed_at.isoformat(),
                "observation_created_at": created_at.isoformat(),
                "available_at": max(observed_at, created_at).isoformat(),
                "actual_stress": max(0.0, min(10.0, stress)),
                "workload": max(0.0, min(1.0, workload)),
                "recovery": max(0.0, min(1.0, recovery)),
            }
        )
    rows.sort(key=lambda row: (row["participant_id"], row["observed_at"]))
    return rows


def _transitions(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build adjacent, causally available transitions within each participant."""

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _ordered(samples):
        grouped[row["participant_id"]].append(row)
    result = []
    for rows in grouped.values():
        for previous, current in zip(rows, rows[1:]):
            hours = (
                _aware(current["observed_at"]) - _aware(previous["observed_at"])
            ).total_seconds() / 3600.0
            if hours <= 0.0 or hours > 8.0:
                continue
            result.append(
                {
                    "previous": previous,
                    "current": current,
                    "hours": hours,
                    "stress_change": current["actual_stress"]
                    - previous["actual_stress"],
                    "workload_change": current["workload"] - previous["workload"],
                }
            )
    return result


def evidence_counts(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = _ordered(samples)
    transitions = _transitions(rows)
    recovery = [
        transition
        for transition in transitions
        if transition["current"]["recovery"] >= 0.25
        and transition["stress_change"] < 0
    ]
    reactivity = [
        transition
        for transition in transitions
        if transition["workload_change"] >= 0.10
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
    y = np.asarray([row["actual_stress"] for row in rows], dtype=float)
    workload_estimate = workload_se = None
    recovery_sensitivity = recovery_sensitivity_se = None
    equilibrium_intercept = None
    candidate_design = np.asarray(
        [[1.0, row["workload"], -row["recovery"]] for row in rows],
        dtype=float,
    )
    if (
        len(rows) >= 8
        and len(levels) >= MINIMUM_WORKLOAD_LEVELS
        and float(np.var([row["recovery"] for row in rows])) > 1e-5
        and int(np.linalg.matrix_rank(candidate_design)) == 3
    ):
        # Full equilibrium equation: S_eq = S* + beta_W W - beta_R R.
        # W and R are estimated jointly so recovery sensitivity cannot leak
        # into either workload sensitivity or the dynamic recovery rate.
        design = candidate_design
        inverse = np.linalg.pinv(design.T @ design)
        coefficient = inverse @ design.T @ y
        residual = y - design @ coefficient
        sigma2 = float(residual @ residual) / max(1, len(y) - 3)
        equilibrium_intercept = max(0.0, min(10.0, float(coefficient[0])))
        workload_estimate = max(0.0, min(6.0, float(coefficient[1])))
        recovery_sensitivity = max(0.0, min(6.0, float(coefficient[2])))
        workload_se = math.sqrt(max(0.0, sigma2 * float(inverse[1, 1])))
        recovery_sensitivity_se = math.sqrt(
            max(0.0, sigma2 * float(inverse[2, 2]))
        )

    reactivity_values = []
    recovery_rate_values = []
    s_star_for_dynamics = (
        baseline
        if baseline is not None
        else equilibrium_intercept
        if equilibrium_intercept is not None
        else DEFAULT_POPULATION["S_star_i"]
    )
    beta_w = (
        workload_estimate
        if workload_estimate is not None
        else DEFAULT_POPULATION["workload_sensitivity_i"]
    )
    beta_r = (
        recovery_sensitivity
        if recovery_sensitivity is not None
        else DEFAULT_POPULATION["recovery_sensitivity_i"]
    )
    for transition in _transitions(rows):
        previous = transition["previous"]
        current = transition["current"]
        hours = float(transition["hours"])
        equilibrium = (
            s_star_for_dynamics
            + beta_w * current["workload"]
            - beta_r * current["recovery"]
        )
        denominator = previous["actual_stress"] - equilibrium
        if abs(denominator) <= 1e-6:
            continue
        # Exact inversion of S(t)=S_eq+(S(0)-S_eq)exp(-kappa*t).
        ratio = (current["actual_stress"] - equilibrium) / denominator
        if not 0.0 < ratio < 1.0:
            continue
        kappa = min(3.0, max(0.0, -math.log(ratio) / hours))
        if equilibrium >= previous["actual_stress"]:
            reactivity_values.append(kappa)
        else:
            recovery_rate_values.append(kappa)
    reactivity, reactivity_se = _mean_se(reactivity_values)
    recovery_rate, recovery_rate_se = _mean_se(recovery_rate_values)
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
            "observation": "joint_equilibrium_regression_beta_W",
        },
        "recovery_sensitivity_i": {
            "estimate": recovery_sensitivity,
            "std_error": recovery_sensitivity_se,
            "sample_count": len(rows),
            "identified": recovery_sensitivity is not None,
            "observation": "joint_equilibrium_regression_beta_R",
        },
        "stress_reactivity_i": {
            "estimate": reactivity,
            "std_error": reactivity_se,
            "sample_count": len(reactivity_values),
            "identified": len(reactivity_values) >= 3 and reactivity is not None,
            "observation": "stress_rise_after_workload_increase",
        },
        "stress_recovery_rate_i": {
            "estimate": recovery_rate,
            "std_error": recovery_rate_se,
            "sample_count": len(recovery_rate_values),
            "identified": len(recovery_rate_values) >= MINIMUM_RECOVERY_EPISODES and recovery_rate is not None,
            "observation": "exact_continuous_time_kappa_down",
        },
    }


def estimate_population_prior(
    samples: Sequence[Mapping[str, Any]],
    *,
    target_participant_id: str | uuid.UUID | None = None,
    knowledge_cutoff: datetime | None = None,
) -> dict[str, Any]:
    target = str(target_participant_id) if target_participant_id is not None else None
    by_participant: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    eligible = _ordered(samples)
    for row in eligible:
        participant = str(row.get("participant_id") or "")
        if target is not None and participant == target:
            continue
        if knowledge_cutoff is not None and _aware(row["available_at"]) >= knowledge_cutoff:
            continue
        by_participant[participant].append(row)
    peer_count = len(by_participant)
    sufficient_peers = peer_count >= 2
    raw = (
        [_raw_individual_estimate(rows) for rows in by_participant.values()]
        if sufficient_peers
        else []
    )
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
            "source": (
                "leave_one_participant_out_peers"
                if estimates
                else "versioned_global_default"
            ),
        }
    prior_source = (
        "leave_one_participant_out_peers"
        if sufficient_peers
        else "versioned_global_default_insufficient_peers"
    )
    peer_sample_count = sum(len(rows) for rows in by_participant.values())
    cutoff_value = knowledge_cutoff.isoformat() if knowledge_cutoff else None
    result["_metadata"] = {
        "source": prior_source,
        "population_prior_source": prior_source,
        "minimum_peer_participant_count": 2,
        "peer_participant_count": peer_count,
        "peer_sample_count": peer_sample_count,
        "population_training_sample_count": peer_sample_count,
        "knowledge_cutoff": cutoff_value,
        "population_knowledge_cutoff": cutoff_value,
        "target_participant_id": target,
        "target_excluded": target is not None,
        "target_excluded_from_prior": target is not None,
        "default_version": "stage5-global-population-default.v1",
    }
    return result


def fit_partial_pooling(
    population_samples: Sequence[Mapping[str, Any]],
    individual_samples: Sequence[Mapping[str, Any]],
    *,
    trait_resilience: float | None = None,
    target_participant_id: str | uuid.UUID | None = None,
    knowledge_cutoff: datetime | None = None,
) -> dict[str, Any]:
    population = estimate_population_prior(
        population_samples,
        target_participant_id=target_participant_id,
        knowledge_cutoff=knowledge_cutoff,
    )
    raw = _raw_individual_estimate(individual_samples)
    values: dict[str, float] = {}
    uncertainty: dict[str, dict[str, Any]] = {}
    for name in PARAMETERS:
        prior_mean = float(population[name]["mean"])
        if trait_resilience is not None:
            bounded_resilience = max(0.0, min(1.0, trait_resilience))
            if name == "recovery_sensitivity_i":
                prior_mean += BRS_RECOVERY_SENSITIVITY_SHIFT * bounded_resilience
            elif name == "stress_recovery_rate_i":
                prior_mean += BRS_RECOVERY_RATE_SHIFT * bounded_resilience
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
        if name != "S_star_i":
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
        "trait_prior": {
            "version": BRS_PRIOR_VERSION,
            "recovery_sensitivity_shift": BRS_RECOVERY_SENSITIVITY_SHIFT,
            "recovery_rate_shift": BRS_RECOVERY_RATE_SHIFT,
        },
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
    def choose(primary: Any, fallback: Any) -> float:
        value = _number(primary)
        return float(value) if value is not None else float(fallback)

    return {
        "S_star_i": baseline if baseline is not None else DEFAULT_POPULATION["S_star_i"],
        "workload_sensitivity_i": choose(
            hierarchical.get("workload_sensitivity_i"),
            choose(ctssm.get("workload_stress_gain"), 28.0) / 10.0,
        ),
        "recovery_sensitivity_i": choose(
            hierarchical.get("recovery_sensitivity_i"),
            choose(ctssm.get("recovery_stress_gain"), 20.0) / 10.0,
        ),
        "stress_reactivity_i": choose(
            hierarchical.get("stress_reactivity_i"),
            choose(
                ctssm.get("stress_reactivity_per_hour"),
                DEFAULT_POPULATION["stress_reactivity_i"],
            ),
        ),
        "stress_recovery_rate_i": choose(
            hierarchical.get("stress_recovery_rate_i"),
            choose(
                ctssm.get("stress_recovery_per_hour"),
                DEFAULT_POPULATION["stress_recovery_rate_i"],
            ),
        ),
    }


def runtime_candidate_parameters(fitted: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(fitted["parameters"])
    return {
        "hierarchical_parameters": value,
        "S_star_init": round(value["S_star_i"] * 10.0, 6),
        "ctssm_params": {
            "workload_stress_gain": round(value["workload_sensitivity_i"] * 10.0, 6),
            "stress_reactivity_per_hour": value["stress_reactivity_i"],
            "stress_recovery_per_hour": value["stress_recovery_rate_i"],
            "recovery_stress_gain": round(value["recovery_sensitivity_i"] * 10.0, 6),
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
    equilibrium = (
        parameters["S_star_i"]
        + parameters["workload_sensitivity_i"] * float(row["workload"])
        - parameters["recovery_sensitivity_i"] * float(row.get("recovery") or 0.0)
    )
    rising = equilibrium >= previous
    rate = (
        parameters["stress_reactivity_i"]
        if rising
        else parameters["stress_recovery_rate_i"]
    )
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
            "formal_promotion_eligible": False,
            "fit_engine": "surrogate_diagnostic_only",
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
        "formal_promotion_eligible": False,
        "fit_engine": "surrogate_diagnostic_only",
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
    residual_metrics["coverage"] = None
    residual_metrics["peak_timing_error_minutes"] = None
    residual_checks = {
        "oot_mae_improved": residual_metrics["mae"] is not None and candidate["mae"] is not None and residual_metrics["mae"] < candidate["mae"],
        "coverage_not_decreased": None,
        "peak_error_not_worse": None,
        "formal_residual_promotion_eligible": False,
        "reason": "trajectory_and_interval_not_recomputed",
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
            "version": "stage5-surrogate-diagnostic-nonpromotion.v1",
            "passed": False,
            "checks": promotion_checks,
            "stable_wins": stable_wins,
            "required_wins": required_wins,
            "formal_promotion_eligible": False,
            "reason": "surrogate_predict_base_is_diagnostic_only",
        },
        "residual_gate": {
            "version": "stage5-residual-diagnostic-gate.v2",
            "passed": False,
            "formal_residual_promotion_eligible": False,
            "reason": "trajectory_and_interval_not_recomputed",
            "checks": residual_checks,
            "display": {
                "ctssm_baseline": candidate,
                "residual_correction": residual_metrics,
                "final_prediction": "shadow_only_not_applied_to_active_runtime",
            },
        },
    }


def _deep_merge_parameters(
    base: Mapping[str, Any] | None, override: Mapping[str, Any] | None
) -> dict[str, Any]:
    result = dict(base or {})
    for key, value in dict(override or {}).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge_parameters(result[key], value)
        else:
            result[key] = value
    return result


def _hierarchical_runtime_parameters(
    values: Mapping[str, float], *, trait_resilience: float | None = None
) -> dict[str, Any]:
    return {
        "hierarchical_parameters": dict(values),
        "S_star_init": round(float(values["S_star_i"]) * 10.0, 6),
        "ctssm_params": {
            "workload_stress_gain": round(
                float(values["workload_sensitivity_i"]) * 10.0, 6
            ),
            "recovery_stress_gain": round(
                float(values["recovery_sensitivity_i"]) * 10.0, 6
            ),
            "stress_reactivity_per_hour": float(
                values["stress_reactivity_i"]
            ),
            "stress_recovery_per_hour": float(
                values["stress_recovery_rate_i"]
            ),
            "trait_resilience_prior": trait_resilience,
        },
    }


def _causal_previous_features(
    sample: Mapping[str, Any],
    observation_history: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve lagged outcomes using only records known strictly before target."""

    target = _aware(sample["observed_at"])
    eligible = []
    for observation in observation_history:
        try:
            observed = _aware(observation.get("observed_at"))
            created = _aware(observation.get("created_at"))
        except (TypeError, ValueError):
            continue
        if observed < target and created < target:
            eligible.append((observed, created, observation))
    eligible.sort(key=lambda value: (value[0], value[1]), reverse=True)
    initial = Stage4CandidateReplayService._frozen_initial_state(sample)

    def resolve(keys: Sequence[str], fallback_key: str) -> tuple[float, dict[str, Any]]:
        for observed, created, observation in eligible:
            payload = dict(observation.get("payload") or {})
            for key in keys:
                value = _number(payload.get(key))
                if value is not None:
                    return max(0.0, min(10.0, value)), {
                        "source": "eligible_prior_observation",
                        "observation_id": observation.get("observation_id"),
                        "observed_at": observed.isoformat(),
                        "created_at": created.isoformat(),
                    }
        return float(initial[fallback_key]), {
            "source": "frozen_initial_state",
            "initial_state_revision": sample.get("initial_state_revision"),
        }

    stress, stress_source = resolve(("stress_0_10", "stress"), "stress_0_10")
    vitality, vitality_source = resolve(
        ("energy_0_10", "vitality_0_10", "energy"), "vitality_0_10"
    )
    return {
        "previous_stress": stress,
        "previous_vitality": vitality,
        "previous_feature_provenance": {
            "target": target.isoformat(),
            "stress": stress_source,
            "vitality": vitality_source,
            "rule": "observed_at_lt_target_and_created_at_lt_target.v1",
        },
    }


def _with_intervention_exclusions(
    samples: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    exposures: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item.get("item_type") not in {
            "care_intervention_exposure",
            "warning_delivery",
        }:
            continue
        metadata = dict(item.get("metadata") or {})
        try:
            sent_at = _aware(metadata.get("sent_at"))
        except (TypeError, ValueError):
            continue
        exposures[str(item.get("participant_id"))].append(
            {
                "item_type": item["item_type"],
                "source_id": str(item.get("source_id") or ""),
                "intervention_type": str(
                    metadata.get("intervention_type") or "warning"
                ),
                "sent_at": sent_at,
            }
        )
    result = []
    excluded = 0
    window = timedelta(minutes=STAGE5_INTERVENTION_EXCLUSION_MINUTES)
    for source in samples:
        row = dict(source)
        target = _aware(row["observed_at"])
        matched = [
            exposure
            for exposure in exposures.get(str(row.get("participant_id")), [])
            if timedelta(0) <= target - exposure["sent_at"] <= window
        ]
        row["treatment_exposure"] = 1.0 if matched else 0.0
        row["exclude_from_natural_dynamics_fit"] = bool(matched)
        row["intervention_exposure"] = [
            {**value, "sent_at": value["sent_at"].isoformat()}
            for value in matched
        ]
        excluded += bool(matched)
        result.append(row)
    return result, {
        "policy_version": "stage5-post-intervention-natural-dynamics-exclusion.v1",
        "window_minutes": STAGE5_INTERVENTION_EXCLUSION_MINUTES,
        "frozen_exposure_count": sum(len(values) for values in exposures.values()),
        "excluded_sample_count": excluded,
        "remaining_sample_count": len(result) - excluded,
    }


def fit_residual_ridge_from_real_replay(
    rows: Sequence[Mapping[str, Any]],
    *,
    ridge: float = 2.0,
    residual_max: float = RESIDUAL_MAX,
) -> dict[str, Any]:
    """Fit the shadow correction only against production-engine predictions."""

    usable = [
        dict(row)
        for row in rows
        if _number(row.get("ctssm_baseline_prediction")) is not None
        and not row.get("exclude_from_natural_dynamics_fit")
    ]
    feature_maps = [_residual_feature_map(row) for row in usable]
    names = sorted({name for values in feature_maps for name in values})
    if not usable or not names:
        return {
            "version": RESIDUAL_VERSION,
            "mode": "shadow",
            "fit_engine": REPLAY_ENGINE_VERSION,
            "feature_names": [],
            "coefficients": [],
            "intercept": 0.0,
            "residual_max": residual_max,
            "sample_count": len(usable),
            "residual_sd": None,
        }
    design = np.asarray(
        [[values.get(name, 0.0) for name in names] for values in feature_maps],
        dtype=float,
    )
    target = np.asarray(
        [
            float(row["actual_stress"])
            - float(row["ctssm_baseline_prediction"])
            for row in usable
        ],
        dtype=float,
    )
    augmented = np.column_stack([np.ones(len(usable)), design])
    penalty = np.eye(augmented.shape[1]) * ridge
    penalty[0, 0] = 0.0
    coefficient = (
        np.linalg.pinv(augmented.T @ augmented + penalty)
        @ augmented.T
        @ target
    )
    errors = target - augmented @ coefficient
    return {
        "version": RESIDUAL_VERSION,
        "mode": "shadow",
        "fit_engine": REPLAY_ENGINE_VERSION,
        "feature_names": names,
        "coefficients": [round(float(value), 8) for value in coefficient[1:]],
        "intercept": round(float(coefficient[0]), 8),
        "ridge_lambda": ridge,
        "residual_max": residual_max,
        "sample_count": len(usable),
        "residual_sd": round(float(np.sqrt(np.mean(errors**2))), 6),
    }


def _residual_shadow_comparison_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Keep point OOT metrics and mark un-recomputed trajectory metrics absent."""

    metrics = comparison_metrics(rows)
    for name in (
        "interval_90_coverage",
        "mean_interval_width",
        "peak_magnitude_error",
        "peak_timing_error_minutes",
        "observed_peak_proxy_magnitude_error",
        "observed_peak_proxy_timing_error_minutes",
    ):
        metrics[name] = None
    metrics["peak_metric_source"] = "trajectory_and_interval_not_recomputed"
    return metrics


class Stage5PersonalizedReplayService:
    """Formal expanding-origin validation through the production CTSSM."""

    FORMAL_ENGINE_VERSION = "stage5-real-ctssm-rolling-replay.v2"

    def __init__(self, extractor: Stage4CandidateReplayService):
        self.extractor = extractor
        self.model = extractor.model
        self.timezone = extractor.timezone

    def _replay(
        self,
        *,
        sample: Mapping[str, Any],
        frozen: Mapping[str, Any],
        parameters: Mapping[str, Any],
        variant: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        target = _aware(sample["observed_at"])
        participant = str(sample.get("participant_id") or "")
        calendar = frozen.get("calendars", {}).get(sample.get("forecast_id")) or {}
        arguments = {
            "observations": self.extractor._known_observations(
                frozen.get("observation_history", {}).get(participant, []), target
            ),
            "calendar_events": self.extractor._calendar_events(calendar),
            "local_date": str(sample["local_date"]),
            "initial_state": self.extractor._frozen_initial_state(sample),
            "sleep_debt_hours": float(sample.get("sleep_debt") or 0.0),
        }
        if variant == "m0":
            result = self.model.predict_baseline_m0(
                baseline_params=dict(parameters), **arguments
            )
        else:
            result = self.model.predict_candidate(
                model_variant=variant,
                candidate_params=dict(parameters),
                **arguments,
            )
        if normalize_model_variant(result.model_variant) != variant:
            raise ValueError("formal replay model family mismatch")
        point_time = str(
            (sample.get("context") or {}).get("forecast_point_time") or ""
        )[:5]
        point = next(
            (
                dict(value)
                for value in result.trajectory
                if str(value.get("time") or "")[:5] == point_time
            ),
            None,
        )
        if point is None:
            return None
        peak = max(
            result.trajectory,
            key=lambda value: float(value.get("stress_0_10") or 0.0),
            default=None,
        )
        return point, {
            "trajectory_peak_stress": (
                float(peak.get("stress_0_10") or 0.0) if peak else None
            ),
            "trajectory_peak_time": str(peak.get("time") or "")[:5]
            if peak
            else None,
            "replayed_model_variant": result.model_variant,
        }

    def validate(
        self,
        *,
        frozen: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
        participant_id: uuid.UUID,
    ) -> dict[str, Any]:
        all_samples, intervention_audit = _with_intervention_exclusions(
            frozen.get("samples", []), items
        )
        target_id = str(participant_id)
        target_rows = _ordered(
            [
                row
                for row in all_samples
                if str(row.get("participant_id")) == target_id
                and not row.get("exclude_from_natural_dynamics_fit")
            ]
        )
        splits = rolling_origin_splits(
            target_rows, minimum_training_days=MINIMUM_TRAINING_DAYS
        )
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
        population_provenance = []
        latest_fit = None
        latest_residual = None
        replay_call_count = 0
        split_information_sets = []
        split_active_variants = []
        for split in splits:
            test_day = date.fromisoformat(split["test_days"][0])
            origin = datetime.combine(test_day, time.min, self.timezone).astimezone(
                timezone.utc
            )
            training = [
                target_rows[index]
                for index in split["train_indices"]
                if _aware(target_rows[index]["available_at"]) < origin
            ]
            testing = [target_rows[index] for index in split["test_indices"]]
            population_training = [
                row
                for row in all_samples
                if str(row.get("participant_id")) != target_id
                and not row.get("exclude_from_natural_dynamics_fit")
                and _aware(row.get("available_at") or row["observed_at"]) < origin
            ]
            split_resilience, resilience_provenance = trait_resilience_as_of(
                items, participant_id, origin
            )
            split_explicit, explicit_provenance = explicit_profile_as_of(
                items, participant_id, origin
            )
            split_current, current_provenance = active_learned_profile_as_of(
                items, participant_id, origin
            )
            variant = str(current_provenance["active_variant"])
            split_active_variants.append(
                {
                    "split_index": split["split_index"],
                    "origin_cutoff": origin.isoformat(),
                    "active_variant": variant,
                    "profile_id": current_provenance.get("profile_id"),
                }
            )
            resilience_provenance["usage"] = "evaluation_trait_resilience"
            explicit_provenance["usage"] = "evaluation_explicit_profile"
            current_provenance["usage"] = (
                "evaluation_current_profile_as_of_origin"
            )
            fitted = fit_partial_pooling(
                population_training,
                training,
                trait_resilience=split_resilience,
                target_participant_id=participant_id,
                knowledge_cutoff=origin,
            )
            latest_fit = fitted
            population_provenance.append(
                {
                    "split_index": split["split_index"],
                    **dict(fitted["population_prior"]["_metadata"]),
                }
            )
            global_values = {
                name: float(fitted["population_prior"][name]["mean"])
                for name in PARAMETERS
            }
            global_parameters = _hierarchical_runtime_parameters(
                global_values, trait_resilience=split_resilience
            )
            candidate_parameters = _hierarchical_runtime_parameters(
                fitted["parameters"], trait_resilience=split_resilience
            )
            comparator_parameters = {
                "global_model": global_parameters,
                "explicit_profile_model": _deep_merge_parameters(
                    global_parameters, split_explicit
                ),
                "current_personalized_model": _deep_merge_parameters(
                    _deep_merge_parameters(global_parameters, split_current),
                    split_explicit,
                ),
                "new_candidate_model": _deep_merge_parameters(
                    candidate_parameters, split_explicit
                ),
            }
            split_information_sets.append(
                {
                    "split_index": split["split_index"],
                    "origin_cutoff": origin.isoformat(),
                    "trait_resilience": resilience_provenance,
                    "explicit_profile": explicit_provenance,
                    "current_learned_profile": current_provenance,
                    "comparator_information_set": (
                        "same_origin_calendar_state_observations_trait_explicit_and_current.v2"
                    ),
                }
            )

            residual_training = []
            for row in training:
                replayed = self._replay(
                    sample=row,
                    frozen=frozen,
                    parameters=comparator_parameters["new_candidate_model"],
                    variant=variant,
                )
                replay_call_count += 1
                if replayed is None:
                    continue
                causal = _causal_previous_features(
                    row,
                    frozen.get("observation_history", {}).get(target_id, []),
                )
                residual_training.append(
                    {
                        **row,
                        **causal,
                        "ctssm_baseline_prediction": float(
                            replayed[0]["stress_0_10"]
                        ),
                    }
                )
            residual = fit_residual_ridge_from_real_replay(residual_training)
            latest_residual = residual
            split_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
            peak_cache: dict[tuple[str, str], dict[str, Any]] = {}
            for row in testing:
                causal = _causal_previous_features(
                    row,
                    frozen.get("observation_history", {}).get(target_id, []),
                )
                candidate_result = None
                for family, parameters in comparator_parameters.items():
                    replayed = self._replay(
                        sample=row,
                        frozen=frozen,
                        parameters=parameters,
                        variant=variant,
                    )
                    replay_call_count += 1
                    if replayed is None:
                        continue
                    point, peak = replayed
                    peak_key = (family, str(row["local_date"]))
                    peak_cache.setdefault(peak_key, peak)
                    interval = dict(point.get("stress_interval_90_0_10") or {})
                    result = {
                        **row,
                        **causal,
                        **peak_cache[peak_key],
                        "predicted_stress": float(point["stress_0_10"]),
                        "prediction_lower": _number(interval.get("lower")),
                        "prediction_upper": _number(interval.get("upper")),
                        "split_index": split["split_index"],
                        "replay_engine": self.FORMAL_ENGINE_VERSION,
                        "origin_cutoff": origin.isoformat(),
                    }
                    predictions[family].append(result)
                    split_rows[family].append(result)
                    if family == "new_candidate_model":
                        candidate_result = result
                if candidate_result is not None:
                    correction = predict_residual(candidate_result, residual)
                    shadow = {
                        **candidate_result,
                        "predicted_stress": max(
                            0.0,
                            min(
                                10.0,
                                float(candidate_result["predicted_stress"])
                                + correction,
                            ),
                        ),
                        "residual_correction": correction,
                        "runtime_applied": False,
                        "prediction_lower": None,
                        "prediction_upper": None,
                        "trajectory_peak_stress": None,
                        "trajectory_peak_time": None,
                    }
                    predictions["candidate_with_residual_shadow"].append(shadow)
                    split_rows["candidate_with_residual_shadow"].append(shadow)
            split_metrics.append(
                {
                    "split_index": split["split_index"],
                    "training_days": split["training_days"],
                    "test_days": split["test_days"],
                    "origin_cutoff": origin.isoformat(),
                    "training_sample_count": len(training),
                    "population_sample_count": len(population_training),
                    "metrics": {
                        name: (
                            _residual_shadow_comparison_metrics(values)
                            if name == "candidate_with_residual_shadow"
                            else comparison_metrics(values)
                        )
                        for name, values in split_rows.items()
                    },
                }
            )
        metrics = {
            name: (
                _residual_shadow_comparison_metrics(values)
                if name == "candidate_with_residual_shadow"
                else comparison_metrics(values)
            )
            for name, values in predictions.items()
        }
        candidate = metrics["new_candidate_model"]
        comparator_names = (
            "global_model",
            "explicit_profile_model",
            "current_personalized_model",
        )
        stable_wins = {
            name: sum(
                1
                for split in split_metrics
                if split["metrics"].get("new_candidate_model", {}).get("mae")
                is not None
                and split["metrics"].get(name, {}).get("mae") is not None
                and split["metrics"]["new_candidate_model"]["mae"]
                < split["metrics"][name]["mae"]
            )
            for name in comparator_names
        }
        required_wins = (
            max(1, math.ceil(len(split_metrics) * 0.6)) if split_metrics else 1
        )
        def strictly_better(left: Any, right: Any) -> bool:
            return left is not None and right is not None and float(left) < float(right)

        checks = {
            "formal_real_replay": bool(split_metrics),
            "same_active_model_family": bool(split_metrics),
            "mae_better_than_global": bool(split_metrics)
            and strictly_better(candidate["mae"], metrics["global_model"]["mae"]),
            "mae_better_than_explicit": bool(split_metrics)
            and strictly_better(candidate["mae"], metrics["explicit_profile_model"]["mae"]),
            "mae_better_than_current_personalized": bool(split_metrics)
            and strictly_better(
                candidate["mae"], metrics["current_personalized_model"]["mae"]
            ),
            "stable_split_improvement": bool(split_metrics)
            and all(value >= required_wins for value in stable_wins.values()),
            "coverage_non_inferior": _gate_non_decrease(
                candidate["interval_90_coverage"],
                metrics["current_personalized_model"]["interval_90_coverage"],
            ),
            "peak_timing_non_inferior": _gate_non_increase(
                candidate["peak_timing_error_minutes"],
                metrics["current_personalized_model"]["peak_timing_error_minutes"],
            ),
        }
        residual_metrics = metrics["candidate_with_residual_shadow"]
        residual_checks = {
            "oot_mae_improved": residual_metrics["mae"] is not None
            and candidate["mae"] is not None
            and residual_metrics["mae"] < candidate["mae"],
            "coverage_not_decreased": None,
            "peak_error_not_worse": None,
            "formal_residual_promotion_eligible": False,
            "reason": "trajectory_and_interval_not_recomputed",
        }
        latest_variant = (
            str(split_active_variants[-1]["active_variant"])
            if split_active_variants
            else "m0"
        )
        return {
            "rolling_origin": {
                "version": "stage5-expanding-local-day-origin.v2",
                "minimum_training_days": MINIMUM_TRAINING_DAYS,
                "split_count": len(split_metrics),
                "splits": split_metrics,
            },
            "comparison": metrics,
            "promotion_gate": {
                "version": PROMOTION_GATE_VERSION,
                "passed": all(checks.values()),
                "checks": checks,
                "stable_wins": stable_wins,
                "required_wins": required_wins,
                "formal_promotion_eligible": True,
            },
            "residual_gate": {
                "version": "stage5-residual-gate.v3-point-shadow",
                "passed": False,
                "formal_residual_promotion_eligible": False,
                "reason": "trajectory_and_interval_not_recomputed",
                "checks": {
                    **residual_checks,
                    "correction_bound": RESIDUAL_MAX,
                    "mode": "shadow",
                },
                "display": {
                    "ctssm_baseline": candidate,
                    "residual_correction": residual_metrics,
                    "final_prediction": "shadow_only_not_applied_to_active_runtime",
                },
            },
            "formal_replay_audit": {
                "engine": self.FORMAL_ENGINE_VERSION,
                "production_engine": REPLAY_ENGINE_VERSION,
                "active_variant": latest_variant,
                "active_variant_rule": "split_origin_causal",
                "active_variant_by_split": split_active_variants,
                "comparator_variants": {
                    name: latest_variant
                    for name in comparator_names + ("new_candidate_model",)
                },
                "comparator_variants_by_split": [
                    {
                        "split_index": row["split_index"],
                        "active_variant": row["active_variant"],
                        "families": {
                            name: row["active_variant"]
                            for name in comparator_names
                            + ("new_candidate_model",)
                        },
                    }
                    for row in split_active_variants
                ],
                "same_calendar_state_and_knowledge_rule": True,
                "simulator_call_count": replay_call_count,
                "population_prior_splits": population_provenance,
                "split_information_sets": split_information_sets,
                "intervention_exclusion": intervention_audit,
                "previous_feature_rule": "observed_and_created_strictly_before_target.v1",
                "surrogate_predict_base_used": False,
            },
            "latest_fit": latest_fit,
            "latest_residual_model": latest_residual,
        }


def trait_resilience_as_of(
    items: Sequence[Mapping[str, Any]],
    participant_id: uuid.UUID,
    knowledge_cutoff: datetime,
    *,
    inclusive: bool = False,
) -> tuple[float | None, dict[str, Any]]:
    """Return the latest BRS available at a causal knowledge boundary."""

    cutoff = _aware(knowledge_cutoff)
    candidates = []
    for item in items:
        if item["item_type"] != "psychometric" or str(
            item["participant_id"]
        ) != str(participant_id):
            continue
        metadata = dict(item["metadata"])
        if str(metadata.get("instrument_name") or "").upper() != "BRS":
            continue
        try:
            administered_at = _aware(metadata.get("administered_at"))
            created_at = _aware(metadata.get("created_at"))
        except (TypeError, ValueError):
            continue
        available_at = max(administered_at, created_at)
        outside_boundary = (
            available_at > cutoff if inclusive else available_at >= cutoff
        )
        if outside_boundary:
            continue
        scores = dict(metadata.get("scores") or {})
        score = next(
            (_number(scores.get(name)) for name in ("mean", "total_mean", "score", "brs_mean") if scores.get(name) is not None),
            None,
        )
        if score is None and _number(scores.get("total")) is not None:
            score = float(scores["total"]) / 6.0
        if score is not None and 1.0 <= score <= 5.0:
            candidates.append(
                (
                    available_at,
                    administered_at,
                    created_at,
                    str(metadata.get("assessment_id") or item.get("source_id") or ""),
                    (score - 1.0) / 4.0,
                )
            )
    selected = max(candidates, default=None)
    provenance = {
        "assessment_id": selected[3] if selected else None,
        "administered_at": selected[1].isoformat() if selected else None,
        "created_at": selected[2].isoformat() if selected else None,
        "available_at": selected[0].isoformat() if selected else None,
        "origin_cutoff": cutoff.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "cutoff_operator": "<=" if inclusive else "<",
        "prior_version": BRS_PRIOR_VERSION,
        "trait_resilience": selected[4] if selected else None,
    }
    return (selected[4] if selected else None), provenance


def explicit_profile_as_of(
    items: Sequence[Mapping[str, Any]],
    participant_id: uuid.UUID,
    knowledge_cutoff: datetime,
    *,
    inclusive: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve frozen explicit model parameters at a causal boundary."""

    cutoff = _aware(knowledge_cutoff)
    candidates = []
    for item in items:
        if item["item_type"] != "participant_profile" or str(
            item["participant_id"]
        ) != str(participant_id):
            continue
        metadata = dict(item["metadata"])
        try:
            created_at = _aware(metadata.get("created_at"))
            version = int(metadata.get("version"))
        except (TypeError, ValueError):
            continue
        outside_boundary = created_at > cutoff if inclusive else created_at >= cutoff
        if outside_boundary:
            continue
        parameters = dict(metadata.get("model_params") or {})
        candidates.append(
            (
                created_at,
                version,
                str(metadata.get("profile_id") or item.get("source_id") or ""),
                parameters,
                str(metadata.get("parameters_hash") or ""),
                str(metadata.get("source") or "participant_profiles"),
            )
        )
    selected = max(candidates, default=None, key=lambda value: (value[0], value[1]))
    provenance = {
        "profile_id": selected[2] if selected else None,
        "version": selected[1] if selected else None,
        "created_at": selected[0].isoformat() if selected else None,
        "parameters_hash": selected[4] if selected else hashlib.sha256(
            b"{}"
        ).hexdigest(),
        "source": selected[5] if selected else None,
        "origin_cutoff": cutoff.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "cutoff_operator": "<=" if inclusive else "<",
        "snapshot_item_type": "participant_profile",
    }
    return (dict(selected[3]) if selected else {}), provenance


def active_learned_profile_as_of(
    items: Sequence[Mapping[str, Any]],
    participant_id: uuid.UUID,
    knowledge_cutoff: datetime,
    *,
    inclusive: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the latest frozen runtime-valid learned profile causally."""

    cutoff = _aware(knowledge_cutoff)
    candidates = []
    for item in items:
        if item["item_type"] != "learned_model_profile" or str(
            item["participant_id"]
        ) != str(participant_id):
            continue
        metadata = dict(item["metadata"])
        if metadata.get("runtime_valid") is not True:
            continue
        try:
            created_at = _aware(metadata.get("created_at"))
            version = int(metadata.get("version"))
        except (TypeError, ValueError):
            continue
        outside_boundary = created_at > cutoff if inclusive else created_at >= cutoff
        if outside_boundary:
            continue
        parameters = dict(metadata.get("parameters") or {})
        selection = dict(
            metadata.get("model_selection")
            or parameters.get("model_selection")
            or {}
        )
        variant = normalize_model_variant(
            metadata.get("active_variant")
            or selection.get("active_variant")
            or "m0"
        )
        candidates.append(
            (
                created_at,
                version,
                str(metadata.get("profile_id") or item.get("source_id") or ""),
                parameters,
                str(metadata.get("parameters_hash") or ""),
                str(metadata.get("model_version") or ""),
                str(metadata.get("validation_status") or ""),
                str(metadata.get("source") or ""),
                variant,
                dict(metadata.get("runtime_validation") or {}),
            )
        )
    selected = max(candidates, default=None, key=lambda value: (value[0], value[1]))
    provenance = {
        "profile_id": selected[2] if selected else None,
        "version": selected[1] if selected else None,
        "created_at": selected[0].isoformat() if selected else None,
        "parameters_hash": selected[4] if selected else hashlib.sha256(
            b"{}"
        ).hexdigest(),
        "model_version": selected[5] if selected else None,
        "validation_status": selected[6] if selected else None,
        "source": selected[7] if selected else "current_m0_fallback",
        "active_variant": selected[8] if selected else "m0",
        "runtime_validation": selected[9] if selected else {
            "runtime_valid": True,
            "reason": "no_historical_active_current_m0_fallback",
        },
        "origin_cutoff": cutoff.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "cutoff_operator": "<=" if inclusive else "<",
        "snapshot_item_type": "learned_model_profile",
        "fallback": selected is None,
    }
    return (dict(selected[3]) if selected else {}), provenance


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
            "recovery_stress_gain": scaled("recovery_sensitivity_i", 10.0),
            "stress_reactivity_per_hour": scaled("stress_reactivity_i"),
            "stress_recovery_per_hour": scaled("stress_recovery_rate_i"),
        },
        "hierarchical_population_prior": {
            "std_error": mean(
                float(fitted["population_prior"][name]["std_dev"])
                for name in PARAMETERS
            )
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
        self.formal_replay = Stage5PersonalizedReplayService(self.extractor)
        self.snapshot_integrity = DatasetSnapshotIntegrityService()

    @staticmethod
    def _view(row: ParameterLearningRun) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "dataset_snapshot_id": str(row.dataset_snapshot_id),
            "model_family": row.model_family,
            "run_kind": row.run_kind,
            "schedule_key": row.schedule_key,
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

    def _resolve_scheduled_integrity_error(
        self,
        error: IntegrityError,
        *,
        participant_id: uuid.UUID,
        schedule_key: str,
    ) -> dict[str, Any]:
        """Return only the exact weekly duplicate; rethrow unrelated failures."""

        with self.database.session() as session:
            existing = session.execute(
                select(ParameterLearningRun).where(
                    ParameterLearningRun.participant_id == participant_id,
                    ParameterLearningRun.model_family == MODEL_FAMILY,
                    ParameterLearningRun.run_kind == "scheduled",
                    ParameterLearningRun.schedule_key == schedule_key,
                )
            ).scalar_one_or_none()
            if existing is None:
                raise error
            return self._view(existing)

    def _snapshot(
        self, snapshot_id: uuid.UUID, participant_id: uuid.UUID
    ) -> tuple[DatasetSnapshot, list[dict[str, Any]], dict[str, Any]]:
        with self.database.session() as session:
            snapshot = session.get(DatasetSnapshot, snapshot_id)
            if snapshot is None:
                raise ValueError("dataset snapshot not found")
            rows = session.execute(
                select(DatasetSnapshotItem).where(DatasetSnapshotItem.dataset_snapshot_id == snapshot_id)
            ).scalars().all()
            integrity = self.snapshot_integrity.verify(
                snapshot,
                rows,
                supported_schema_versions={DATASET_SCHEMA_V7},
                participant_id=participant_id,
            )
            return snapshot, list(integrity["items"]), {
                name: value
                for name, value in integrity.items()
                if name not in {"items", "participant_ids"}
            }

    def _active_identity(
        self, active: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        view = dict(active or {})
        parameters = dict(view.get("parameters") or {})
        selection = dict(parameters.get("model_selection") or {})
        variant = normalize_model_variant(selection.get("active_variant") or "m0")
        return {
            "profile_id": view.get("id"),
            "profile_version": view.get("version"),
            "parameters_hash": promotion_parameters_hash(parameters),
            "active_variant": variant,
            "model_spec_version": (
                f"{self.extractor.model.MODEL_SPEC_VERSION}:{variant}"
            ),
            "stage4_promotion_decision_id": selection.get("promotion_decision_id"),
            "stage4_status": selection.get("status"),
        }

    def train_snapshot(
        self,
        snapshot_id: uuid.UUID,
        participant_id: uuid.UUID,
        *,
        run_kind: str = "manual",
        schedule_key: str | None = None,
    ) -> dict[str, Any]:
        if (run_kind == "manual") != (schedule_key is None):
            raise ValueError("manual runs require no schedule key; scheduled runs require one")
        if run_kind not in {"manual", "scheduled"}:
            raise ValueError("invalid parameter learning run kind")
        snapshot, items, snapshot_audit = self._snapshot(snapshot_id, participant_id)
        frozen = self.extractor._extract(items, None)
        extracted_population, intervention_audit = _with_intervention_exclusions(
            frozen["samples"], items
        )
        natural_population = [
            row
            for row in extracted_population
            if not row.get("exclude_from_natural_dynamics_fit")
            and max(
                _aware(row["observed_at"]),
                _aware(
                    row.get("observation_created_at")
                    or row.get("created_at")
                    or row["observed_at"]
                ),
            )
            < _aware(snapshot.observation_cutoff)
        ]
        individual = [
            row
            for row in natural_population
            if str(row.get("participant_id")) == str(participant_id)
        ]
        gate = minimum_data_gate(individual)
        resilience, resilience_provenance = trait_resilience_as_of(
            items,
            participant_id,
            snapshot.observation_cutoff,
            inclusive=True,
        )
        resilience_provenance["usage"] = "deployment_trait_resilience"
        fitted = fit_partial_pooling(
            natural_population,
            individual,
            trait_resilience=resilience,
            target_participant_id=participant_id,
            knowledge_cutoff=snapshot.observation_cutoff,
        )
        current = self.learned_profiles.runtime_active(participant_id)
        before = dict((current or {}).get("parameters") or {})
        base_active_identity = self._active_identity(current)
        _deployment_explicit, explicit_provenance = explicit_profile_as_of(
            items,
            participant_id,
            snapshot.observation_cutoff,
            inclusive=True,
        )
        explicit_provenance["usage"] = "deployment_explicit_profile"
        validation = self.formal_replay.validate(
            frozen=frozen,
            items=items,
            participant_id=participant_id,
        )
        residual = dict(validation.get("latest_residual_model") or {})
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
            "deployment_trait_resilience_provenance": resilience_provenance,
            "explicit_profile_provenance": explicit_provenance,
            "schedule": "weekly_dataset_snapshot" if run_kind == "scheduled" else "manual_dataset_snapshot",
            "run_kind": run_kind,
            "schedule_key": schedule_key,
            "dataset_snapshot_integrity": snapshot_audit,
            "intervention_exclusion": intervention_audit,
            "base_active_identity": base_active_identity,
            "deployment_base_active_identity_at_train_time": {
                **base_active_identity,
                "usage": "deployment_base_active_identity_at_train_time",
            },
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
            "active_variant": base_active_identity["active_variant"],
            "model_spec_version": base_active_identity["model_spec_version"],
        }
        profile_parameters = {**candidate, "model_selection": model_selection}
        window_start = min((date.fromisoformat(str(row["local_date"])) for row in individual), default=snapshot.date_start)
        window_end = max((date.fromisoformat(str(row["local_date"])) for row in individual), default=snapshot.date_end)
        try:
            with self.database.session() as session:
                session.add(
                    ParameterLearningRun(
                    id=run_id,
                    participant_id=participant_id,
                    dataset_snapshot_id=snapshot_id,
                    model_family=MODEL_FAMILY,
                    run_kind=run_kind,
                    schedule_key=schedule_key,
                    parameters_before=before,
                    parameters_candidate=candidate,
                    training_metrics=training_metrics,
                    validation_metrics=validation_metrics,
                    sample_count=len(individual),
                    status=status,
                    )
                )
                # Force the partial unique constraint before creating a profile.
                session.flush()
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
                    model_version="mindflow-ctssm-runtime-v11",
                    validation_status="candidate" if status == "candidate" else "rejected",
                )
        except IntegrityError as error:
            if run_kind != "scheduled":
                raise
            return self._resolve_scheduled_integrity_error(
                error,
                participant_id=participant_id,
                schedule_key=str(schedule_key),
            )
        with self.database.session() as session:
            return self._view(session.get(ParameterLearningRun, run_id))

    def promote(self, run_id: uuid.UUID) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(ParameterLearningRun, run_id, with_for_update=True)
            if run is None:
                raise ValueError("parameter learning run not found")
            if run.status != "candidate":
                raise ValueError("only a candidate parameter learning run can be promoted")
            if run.model_family != MODEL_FAMILY:
                raise ValueError("unsupported_parameter_learning_candidate_version")
            validation = dict(run.validation_metrics or {})
            promotion_gate = dict(validation.get("promotion_gate") or {})
            if not bool(promotion_gate.get("passed")):
                raise ValueError("candidate did not pass personalized promotion gate")
            if promotion_gate.get("formal_promotion_eligible") is not True:
                raise ValueError("candidate was not validated by formal real replay")
            if promotion_gate.get("version") != PROMOTION_GATE_VERSION:
                raise ValueError("unsupported personalized promotion gate version")
            formal_audit = dict(validation.get("formal_replay_audit") or {})
            if formal_audit.get("engine") != self.formal_replay.FORMAL_ENGINE_VERSION:
                raise ValueError("unsupported Stage-5 formal replay engine")
            if not bool((run.training_metrics.get("minimum_data_gate") or {}).get("passed")):
                raise ValueError("candidate did not pass minimum data gate")
            candidate = dict(run.parameters_candidate or {})
            hierarchical = dict(candidate.get("hierarchical_parameters") or {})
            if set(PARAMETERS) - set(hierarchical):
                raise ValueError("candidate is missing separated Stage-5 parameters")
            uncertainty = dict(validation.get("uncertainty") or {})
            if session.get(Participant, run.participant_id, with_for_update=True) is None:
                raise ValueError("participant not found")
            current = self.learned_profiles.runtime_active_in_session(
                session, run.participant_id, for_update=True
            )
            frozen_identity = dict(
                (run.training_metrics or {}).get("base_active_identity") or {}
            )
            if frozen_identity != self._active_identity(current):
                raise ValueError("stale_parameter_learning_candidate")
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
                model_version="mindflow-ctssm-runtime-v11",
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
        iso = through.isocalendar()
        schedule_key = f"{iso.year}-W{iso.week:02d}"
        with self.database.session() as session:
            latest = session.execute(
                select(ParameterLearningRun)
                .where(
                    ParameterLearningRun.participant_id == participant_id,
                    ParameterLearningRun.model_family == MODEL_FAMILY,
                    ParameterLearningRun.run_kind == "scheduled",
                    ParameterLearningRun.schedule_key == schedule_key,
                )
                .order_by(desc(ParameterLearningRun.created_at))
                .limit(1)
            ).scalar_one_or_none()
            if latest is not None:
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
                    DatasetSnapshot.schema_version == DATASET_SCHEMA_V7,
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
        run = self.train_snapshot(
            uuid.UUID(snapshot["id"]),
            participant_id,
            run_kind="scheduled",
            schedule_key=schedule_key,
        )
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
