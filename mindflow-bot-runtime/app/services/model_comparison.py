"""Stage-4 CTSSM parameter estimation, rolling splits, metrics and gate.

The functions in this module are deliberately data-source agnostic.  Callers
must supply rows frozen by a Dataset Snapshot; no live observation lookup is
performed here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence


MODEL_FAMILIES = (
    "current_m0",
    "workload_aware_m0",
    "m1",
    "m2",
    "m3",
)
MODEL_VARIANT_BY_FAMILY = {
    "current_m0": "m0",
    "workload_aware_m0": "wm0",
    "m1": "m1",
    "m2": "m2",
    "m3": "m3",
}
ROLLING_ORIGIN_VERSION = "rolling-origin-knowledge-causal.v2"
PROMOTION_GATE_VERSION = "ctssm-promotion-gate.v1"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def trait_resilience_prior(scores: Mapping[str, Any] | None) -> float | None:
    """Normalize a BRS score into [0, 1] without inventing a score.

    The repository accepts instrument-version-specific score payloads, so the
    canonical mean may be stored under several established field names.
    """

    if not isinstance(scores, Mapping):
        return None
    mean_value = next(
        (
            _number(scores.get(key))
            for key in ("mean", "total_mean", "score", "brs_mean")
            if scores.get(key) is not None
        ),
        None,
    )
    if mean_value is None and scores.get("total") is not None:
        total = _number(scores.get("total"))
        if total is None or not 6.0 <= total <= 30.0:
            return None
        mean_value = total / 6.0
    if mean_value is None or not 1.0 <= mean_value <= 5.0:
        return None
    return round((mean_value - 1.0) / 4.0, 6)


def rolling_origin_splits(
    samples: Sequence[Mapping[str, Any]],
    *,
    minimum_training_days: int = 2,
) -> list[dict[str, Any]]:
    """Return expanding-window day splits, never training on the test day."""

    by_day: dict[str, list[int]] = defaultdict(list)
    for index, sample in enumerate(samples):
        day = str(sample.get("local_date") or "")
        if day:
            by_day[day].append(index)
    days = sorted(by_day)
    result = []
    for origin in range(max(1, minimum_training_days), len(days)):
        training_days = days[:origin]
        test_day = days[origin]
        result.append(
            {
                "split_index": len(result),
                "training_days": training_days,
                "test_days": [test_day],
                "train_indices": [
                    index for day in training_days for index in by_day[day]
                ],
                "test_indices": list(by_day[test_day]),
            }
        )
    return result


def _solve_three_by_three(matrix: list[list[float]], vector: list[float]) -> list[float]:
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-9:
            raise ValueError("parameter design matrix is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                left - factor * right
                for left, right in zip(augmented[row], augmented[column])
            ]
    return [augmented[index][3] for index in range(3)]


def _invert_three_by_three(matrix: list[list[float]]) -> list[list[float]]:
    augmented = [
        row[:] + [1.0 if row_index == column else 0.0 for column in range(3)]
        for row_index, row in enumerate(matrix)
    ]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("parameter design matrix is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                left - factor * right
                for left, right in zip(augmented[row], augmented[column])
            ]
    return [row[3:] for row in augmented]


def _infinity_norm(matrix: Sequence[Sequence[float]]) -> float:
    return max((sum(abs(value) for value in row) for row in matrix), default=0.0)


def estimate_reactivity_and_recovery(
    samples: Sequence[Mapping[str, Any]],
    *,
    trait_resilience: float | None = None,
) -> dict[str, Any]:
    """Estimate identifiable workload/recovery coefficients with a BRS prior."""

    usable = []
    for sample in samples:
        stress = _number(sample.get("actual_stress"))
        workload = _number(sample.get("workload"))
        recovery = _number(sample.get("recovery"))
        if stress is None or workload is None or recovery is None:
            continue
        usable.append((stress, max(0.0, min(1.0, workload)), max(0.0, min(1.0, recovery))))

    # Weak population ridge prior makes sparse participant estimates explicit
    # and bounded; it is not counted as an observed sample.
    ridge = 0.35
    matrix = [[0.0] * 3 for _ in range(3)]
    vector = [0.0] * 3
    for stress, workload, recovery in usable:
        row = [1.0, workload, -recovery]
        for i in range(3):
            vector[i] += row[i] * stress
            for j in range(3):
                matrix[i][j] += row[i] * row[j]
    design_matrix = [row[:] for row in matrix]
    population = [5.0, 2.8, 1.4]
    resilience = (
        None
        if trait_resilience is None
        else max(0.0, min(1.0, float(trait_resilience)))
    )
    if resilience is not None:
        # rho_i shifts only the recovery prior, as required by the model.
        population[2] = 1.0 + 1.0 * resilience
    for index in range(3):
        matrix[index][index] += ridge
        vector[index] += ridge * population[index]
    coefficients = _solve_three_by_three(matrix, vector)
    baseline = max(0.0, min(10.0, coefficients[0]))
    beta_workload = max(0.0, min(6.0, coefficients[1]))
    beta_recovery = max(0.0, min(6.0, coefficients[2]))
    boundary_clipped = any(
        raw != clipped
        for raw, clipped in zip(
            coefficients, (baseline, beta_workload, beta_recovery)
        )
    )

    residuals = [
        stress
        - (
            coefficients[0]
            + coefficients[1] * workload
            - coefficients[2] * recovery
        )
        for stress, workload, recovery in usable
    ]
    residual_sd = (
        math.sqrt(sum(value * value for value in residuals) / max(1, len(residuals) - 3))
        if residuals
        else None
    )
    inverse_ridge = _invert_three_by_three(matrix)
    coefficient_standard_errors = [
        (
            math.sqrt(max(0.0, residual_sd * residual_sd * inverse_ridge[index][index]))
            if residual_sd is not None
            else None
        )
        for index in range(3)
    ]
    try:
        inverse_design = _invert_three_by_three(design_matrix)
        condition_number = _infinity_norm(design_matrix) * _infinity_norm(
            inverse_design
        )
    except ValueError:
        condition_number = 1e12

    workloads = [value[1] for value in usable]
    recoveries = [value[2] for value in usable]

    def variance(values: Sequence[float]) -> float:
        if not values:
            return 0.0
        center = sum(values) / len(values)
        return sum((value - center) ** 2 for value in values) / len(values)

    workload_variance = variance(workloads)
    recovery_variance = variance(recoveries)
    if workload_variance > 0 and recovery_variance > 0:
        workload_mean = sum(workloads) / len(workloads)
        recovery_mean = sum(recoveries) / len(recoveries)
        correlation = sum(
            (workload - workload_mean) * (recovery - recovery_mean)
            for workload, recovery in zip(workloads, recoveries)
        ) / (
            len(usable) * math.sqrt(workload_variance * recovery_variance)
        )
    else:
        correlation = None
    absolute_correlation = abs(correlation) if correlation is not None else 1.0
    if (
        len(usable) < 3
        or workload_variance < 1e-4
        or recovery_variance < 1e-4
        or absolute_correlation >= 0.98
        or condition_number >= 1e6
    ):
        identifiability_status = "not_identified"
    elif (
        len(usable) < 8
        or absolute_correlation >= 0.90
        or condition_number >= 1e3
    ):
        identifiability_status = "weak"
    else:
        identifiability_status = "identified"
    return {
        "stress_baseline_0_10": round(baseline, 4),
        "workload_reactivity_beta": round(beta_workload, 4),
        "recovery_beta": round(beta_recovery, 4),
        "trait_resilience_prior": resilience,
        "sample_count": len(usable),
        "residual_sd": round(residual_sd, 4) if residual_sd is not None else None,
        "uncertainty_method": "ridge-posterior-covariance.v1",
        "ridge_lambda": ridge,
        "design_condition_number": round(min(condition_number, 1e12), 4),
        "workload_variance": round(workload_variance, 8),
        "recovery_variance": round(recovery_variance, 8),
        "workload_recovery_correlation": (
            round(correlation, 6) if correlation is not None else None
        ),
        "identifiability_status": identifiability_status,
        "boundary_clipped": boundary_clipped,
        "uncertainty": {
            "stress_baseline_0_10": {
                "std_error": coefficient_standard_errors[0]
            },
            "workload_reactivity_beta": {
                "std_error": coefficient_standard_errors[1]
            },
            "recovery_beta": {"std_error": coefficient_standard_errors[2]},
        },
    }


def observed_recovery_efficiency(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Estimate post-episode stress decline per hour from longitudinal EMA."""

    rates: list[float] = []
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample.get("participant_id") or "")].append(sample)
    for participant_samples in grouped.values():
        ordered = sorted(
            participant_samples,
            key=lambda item: str(item.get("observed_at") or ""),
        )
        for previous, current in zip(ordered, ordered[1:]):
            try:
                left = datetime.fromisoformat(str(previous["observed_at"]).replace("Z", "+00:00"))
                right = datetime.fromisoformat(str(current["observed_at"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            hours = (right - left).total_seconds() / 3600.0
            if not 0.0 < hours <= 6.0:
                continue
            left_stress = _number(previous.get("actual_stress"))
            right_stress = _number(current.get("actual_stress"))
            left_workload = _number(previous.get("workload"))
            right_workload = _number(current.get("workload"))
            recovery = _number(current.get("recovery"))
            if None in {left_stress, right_stress, left_workload, right_workload, recovery}:
                continue
            if (left_stress >= 7.0 or left_workload >= 0.7) and (
                right_workload <= left_workload and recovery > 0.0
            ):
                rates.append(max(-5.0, min(5.0, (left_stress - right_stress) / hours)))
    return {
        "observed_recovery_efficiency_per_hour": (
            round(median(rates), 4) if rates else None
        ),
        "episode_transition_count": len(rates),
    }


def estimate_response_rates(
    samples: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Estimate continuous-time rise/recovery rates from adjacent EMA rows."""

    baseline = _number(parameters.get("stress_baseline_0_10"))
    beta_w = _number(parameters.get("workload_reactivity_beta"))
    beta_r = _number(parameters.get("recovery_beta"))
    if None in {baseline, beta_w, beta_r}:
        return {
            "stress_reactivity_per_hour": None,
            "stress_recovery_per_hour": None,
            "response_transition_count": 0,
            "recovery_transition_count": 0,
        }
    rise, fall = [], []
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample.get("participant_id") or "")].append(sample)
    for participant_samples in grouped.values():
        ordered = sorted(
            participant_samples,
            key=lambda item: str(item.get("observed_at") or ""),
        )
        for previous, current in zip(ordered, ordered[1:]):
            try:
                left_time = datetime.fromisoformat(
                    str(previous["observed_at"]).replace("Z", "+00:00")
                )
                right_time = datetime.fromisoformat(
                    str(current["observed_at"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                continue
            hours = (right_time - left_time).total_seconds() / 3600.0
            if not 0.0 < hours <= 6.0:
                continue
            left = _number(previous.get("actual_stress"))
            right = _number(current.get("actual_stress"))
            workload = _number(current.get("workload"))
            recovery = _number(current.get("recovery"))
            if None in {left, right, workload, recovery}:
                continue
            equilibrium = baseline + beta_w * workload - beta_r * recovery
            initial_gap, final_gap = left - equilibrium, right - equilibrium
            if initial_gap == 0 or initial_gap * final_gap <= 0:
                continue
            ratio = abs(final_gap / initial_gap)
            if not 0.0 < ratio < 1.0:
                continue
            rate = max(0.05, min(4.0, -math.log(ratio) / hours))
            (rise if equilibrium >= left else fall).append(rate)
    return {
        "stress_reactivity_per_hour": round(median(rise), 4) if rise else None,
        "stress_recovery_per_hour": round(median(fall), 4) if fall else None,
        "response_transition_count": len(rise),
        "recovery_transition_count": len(fall),
    }


def _pr_auc(actual: Sequence[bool], scores: Sequence[float]) -> float | None:
    positives = sum(actual)
    if positives == 0 or not actual:
        return None
    ordered = sorted(zip(scores, actual), key=lambda item: item[0], reverse=True)
    tp = fp = 0
    previous_recall = 0.0
    area = 0.0
    for _, label in ordered:
        tp += int(label)
        fp += int(not label)
        recall = tp / positives
        precision = tp / max(1, tp + fp)
        area += (recall - previous_recall) * precision
        previous_recall = recall
    return area


def comparison_metrics(
    predictions: Sequence[Mapping[str, Any]],
    *,
    high_stress_threshold: float = 7.0,
) -> dict[str, Any]:
    """Compute the complete Stage-4 comparison metric set."""

    rows = []
    for item in predictions:
        actual, predicted = _number(item.get("actual_stress")), _number(item.get("predicted_stress"))
        if actual is not None and predicted is not None:
            rows.append((item, actual, predicted))
    result = {
        "sample_count": len(rows),
        "mae": None,
        "rmse": None,
        "median_absolute_error": None,
        "peak_magnitude_error": None,
        "peak_timing_error_minutes": None,
        "observed_peak_proxy_magnitude_error": None,
        "observed_peak_proxy_timing_error_minutes": None,
        "peak_metric_source": None,
        "interval_90_coverage": None,
        "mean_interval_width": None,
        "high_stress_precision": None,
        "high_stress_recall": None,
        "pr_auc": None,
    }
    if not rows:
        return result
    errors = [predicted - actual for _, actual, predicted in rows]
    intervals = []
    for item, actual, _ in rows:
        lower, upper = _number(item.get("prediction_lower")), _number(item.get("prediction_upper"))
        if lower is not None and upper is not None and lower <= upper:
            intervals.append((lower, upper, actual))
    actual_high = [actual >= high_stress_threshold for _, actual, _ in rows]
    predicted_high = [predicted >= high_stress_threshold for _, _, predicted in rows]
    tp = sum(left and right for left, right in zip(actual_high, predicted_high))
    fp = sum((not left) and right for left, right in zip(actual_high, predicted_high))
    fn = sum(left and (not right) for left, right in zip(actual_high, predicted_high))

    groups: dict[tuple[str, str], list[tuple[Mapping[str, Any], float, float]]] = defaultdict(list)
    for row in rows:
        item = row[0]
        groups[(str(item.get("participant_id") or ""), str(item.get("local_date") or ""))].append(row)
    magnitude, timing = [], []
    observed_proxy_magnitude, observed_proxy_timing = [], []
    for group in groups.values():
        if len(group) < 2:
            continue
        actual_peak = max(group, key=lambda row: row[1])
        predicted_peak = max(group, key=lambda row: row[2])
        observed_proxy_magnitude.append(abs(actual_peak[1] - predicted_peak[2]))
        proxy_timing_value = None
        try:
            actual_time = datetime.fromisoformat(str(actual_peak[0]["observed_at"]).replace("Z", "+00:00"))
            predicted_time = datetime.fromisoformat(str(predicted_peak[0]["observed_at"]).replace("Z", "+00:00"))
            proxy_timing_value = abs(
                (actual_time - predicted_time).total_seconds()
            ) / 60.0
            observed_proxy_timing.append(proxy_timing_value)
        except (KeyError, TypeError, ValueError):
            pass
        trajectory_peak = _number(group[0][0].get("trajectory_peak_stress"))
        trajectory_peak_time = str(
            group[0][0].get("trajectory_peak_time") or ""
        )[:5]
        if trajectory_peak is None or not trajectory_peak_time:
            magnitude.append(abs(actual_peak[1] - predicted_peak[2]))
            if proxy_timing_value is not None:
                timing.append(proxy_timing_value)
            continue
        magnitude.append(abs(actual_peak[1] - trajectory_peak))
        try:
            actual_time = datetime.fromisoformat(
                str(actual_peak[0]["observed_at"]).replace("Z", "+00:00")
            )
            hour, minute = (int(part) for part in trajectory_peak_time.split(":"))
            trajectory_time = actual_time.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            timing.append(
                abs((actual_time - trajectory_time).total_seconds()) / 60.0
            )
        except (KeyError, TypeError, ValueError):
            pass
    pr_auc = _pr_auc(actual_high, [predicted for _, _, predicted in rows])
    result.update(
        {
            "mae": round(mean(abs(value) for value in errors), 4),
            "rmse": round(math.sqrt(mean(value * value for value in errors)), 4),
            "median_absolute_error": round(median(abs(value) for value in errors), 4),
            "peak_magnitude_error": round(mean(magnitude), 4) if magnitude else None,
            "peak_timing_error_minutes": round(mean(timing), 2) if timing else None,
            "observed_peak_proxy_magnitude_error": round(mean(observed_proxy_magnitude), 4) if observed_proxy_magnitude else None,
            "observed_peak_proxy_timing_error_minutes": round(mean(observed_proxy_timing), 2) if observed_proxy_timing else None,
            "peak_metric_source": (
                "observed_ema_peak_vs_full_trajectory_peak"
                if any(row[0].get("trajectory_peak_time") for row in rows)
                else "observed_ema_peak_proxy"
            ),
            "interval_90_coverage": round(mean(lower <= actual <= upper for lower, upper, actual in intervals), 4) if intervals else None,
            "mean_interval_width": round(mean(upper - lower for lower, upper, _ in intervals), 4) if intervals else None,
            "high_stress_precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "high_stress_recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "pr_auc": round(pr_auc, 4) if pr_auc is not None else None,
        }
    )
    return result


def promotion_gate(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    minimum_relative_mae_improvement: float = 0.03,
) -> dict[str, Any]:
    """Apply the four mandatory non-inferiority/improvement checks."""

    baseline_mae, candidate_mae = _number(baseline.get("mae")), _number(candidate.get("mae"))
    relative = (
        (baseline_mae - candidate_mae) / baseline_mae
        if baseline_mae is not None and candidate_mae is not None and baseline_mae > 0
        else None
    )

    def non_inferior(name: str, *, lower_is_better: bool = False) -> bool:
        left, right = _number(baseline.get(name)), _number(candidate.get(name))
        if left is None or right is None:
            return False
        return right <= left if lower_is_better else right >= left

    checks = {
        "relative_mae_improvement": relative is not None and relative >= minimum_relative_mae_improvement,
        "interval_coverage_non_inferior": non_inferior("interval_90_coverage"),
        "peak_timing_non_inferior": non_inferior("peak_timing_error_minutes", lower_is_better=True),
        "high_stress_recall_non_inferior": non_inferior("high_stress_recall"),
    }
    # The Stage-4 equations may only be promoted when the states and
    # parameters they add are backed by frozen observable evidence.  Keep
    # this optional for callers evaluating the generic metric gate alone.
    observable_support = candidate.get("observable_support")
    if isinstance(observable_support, Mapping):
        checks["observable_support"] = bool(observable_support.get("supported"))
    return {
        "gate_version": PROMOTION_GATE_VERSION,
        "passed": all(checks.values()),
        "checks": checks,
        "relative_mae_improvement": round(relative, 6) if relative is not None else None,
        "sample_count": int(candidate.get("sample_count") or 0),
        "participant_effect": candidate.get("participant_effect"),
    }
