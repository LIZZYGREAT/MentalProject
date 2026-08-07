"""Engineering and posterior-predictive validation protocol from the paper."""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from algorithm.dynamic_state_model import MODEL_VARIANTS, normalize_model_variant
from algorithm.time_utils import time_to_minutes
from calibration.metrics import closest_result_at
from calibration.simulation_runner import run_simulation_for_calibration


def run_engineering_validation_protocol(
    sample: Mapping[str, Any],
    base_params: Mapping[str, Any],
    *,
    model_variant: Any = "m0",
) -> Dict[str, Any]:
    """Run permutation, cancellation, completion, and step-size checks.

    These checks can catch implementation errors.  They do not establish
    psychological validity and are explicitly labelled as engineering checks.
    """

    variant = normalize_model_variant(model_variant)
    params = _candidate_params(base_params, variant)
    baseline = _simulate(sample, params)

    permuted_sample = deepcopy(dict(sample))
    permuted_sample["events"] = _permute_event_times(
        permuted_sample.get("events", [])
    )
    permuted = _simulate(permuted_sample, params)

    cancelled_sample = deepcopy(dict(sample))
    cancelled_events = cancelled_sample.get("events", [])
    if cancelled_events:
        event = cancelled_events[0]
        event["cancelled"] = True
        event["cancelled_at"] = _minutes_to_time(
            max(
                0,
                time_to_minutes(
                    str(event.get("start_time") or event.get("start") or "00:00")[-5:]
                )
                - 30,
            )
        )
    cancelled = _simulate(cancelled_sample, params)

    completed_sample = deepcopy(dict(sample))
    unfinished_sample = deepcopy(dict(sample))
    for completed_event in completed_sample.get("events", []):
        completed_event.setdefault("objective", {})["unfinished"] = 0.0
    for unfinished_event in unfinished_sample.get("events", []):
        unfinished_event.setdefault("objective", {})["unfinished"] = 1.0
    completed = _simulate(completed_sample, params)
    unfinished = _simulate(unfinished_sample, params)

    step_results: Dict[int, List[Dict[str, Any]]] = {}
    for step in (1, 5, 10):
        step_params = deepcopy(params)
        step_params["time_step"] = step
        step_results[step] = _simulate(sample, step_params)["results"]
    step_max_difference = _max_aligned_difference(step_results)

    baseline_peak = _peak_stress(baseline["results"])
    cancelled_peak = _peak_stress(cancelled["results"])
    permutation_delta = _mean_absolute_trajectory_difference(
        baseline["results"],
        permuted["results"],
    )
    completed_post = _post_event_mean(completed_sample, completed["results"])
    unfinished_post = _post_event_mean(unfinished_sample, unfinished["results"])
    return {
        "schema_version": "engineering_validation_protocol.v1",
        "model_variant": MODEL_VARIANTS[variant]["canonical"],
        "evidence_type": "engineering_sanity_check_not_empirical_validation",
        "event_time_permutation": {
            "mean_absolute_trajectory_difference": permutation_delta,
            "events_have_incremental_timing_signal": permutation_delta > 0.5,
        },
        "event_cancellation": {
            "baseline_peak_stress": baseline_peak,
            "cancelled_peak_stress": cancelled_peak,
            "cancelled_peak_reduced": cancelled_peak < baseline_peak,
        },
        "completion_status": {
            "completed_post_event_mean_stress": completed_post,
            "unfinished_post_event_mean_stress": unfinished_post,
            "unfinished_recovery_is_slower": unfinished_post > completed_post,
        },
        "step_consistency": {
            "steps_minutes": [1, 5, 10],
            "max_aligned_state_difference": step_max_difference,
            "within_tolerance": step_max_difference <= 0.25,
        },
    }


def posterior_predictive_checks(
    observed: Sequence[float],
    simulated_draws: Iterable[Sequence[float]],
) -> Dict[str, Any]:
    """Compare summary statistics when probabilistic draws become available."""

    observed_values = [float(value) for value in observed]
    draws = [[float(value) for value in draw] for draw in simulated_draws]
    observed_summary = _series_summary(observed_values)
    draw_summaries = [_series_summary(draw) for draw in draws if draw]
    return {
        "schema_version": "posterior_predictive_check.v1",
        "observed": observed_summary,
        "simulated_summary_medians": {
            key: _median(
                [summary[key] for summary in draw_summaries if summary.get(key) is not None]
            )
            for key in ("mean", "variance", "lag1_autocorrelation", "peak")
        },
        "draw_count": len(draw_summaries),
        "status": "evaluated" if observed_values and draw_summaries else "insufficient_draws",
    }


def _candidate_params(base_params: Mapping[str, Any], variant: str) -> Dict[str, Any]:
    params = deepcopy(dict(base_params))
    params["model_family"] = MODEL_VARIANTS[variant]["canonical"]
    params["model_selection"] = {
        **dict(params.get("model_selection", {}) or {}),
        "active_variant": variant,
        "status": "research_candidate_run",
    }
    return params


def _simulate(sample: Mapping[str, Any], params: Mapping[str, Any]) -> Dict[str, Any]:
    return run_simulation_for_calibration(
        date_str=str(sample.get("date")),
        events_json=deepcopy(list(sample.get("events", []))),
        user_params=deepcopy(dict(params)),
        yesterday_state=sample.get("yesterday_state"),
        weave_routines=bool(sample.get("weave_routines", False)),
        observations=sample.get("online_observations", []),
        sleep_context=sample.get("sleep_context"),
    )


def _permute_event_times(events: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    copied = deepcopy(list(events))
    times = [
        (event.get("start_time") or event.get("start"), event.get("end_time") or event.get("end"))
        for event in copied
    ]
    if len(times) > 1:
        times = times[1:] + times[:1]
    elif times:
        start, end = times[0]
        duration = max(5, time_to_minutes(str(end)[-5:]) - time_to_minutes(str(start)[-5:]))
        shifted_start = (time_to_minutes(str(start)[-5:]) + 180) % 1440
        times = [(_minutes_to_time(shifted_start), _minutes_to_time((shifted_start + duration) % 1440))]
    for event, (start, end) in zip(copied, times):
        if "start_time" in event:
            event["start_time"] = start
            event["end_time"] = end
        else:
            event["start"] = start
            event["end"] = end
    return copied


def _max_aligned_difference(step_results: Mapping[int, Sequence[Dict[str, Any]]]) -> float:
    maximum = 0.0
    for time in ("06:00", "09:00", "12:00", "15:00", "18:00", "21:00"):
        reference = closest_result_at(list(step_results[5]), time)
        if reference is None:
            continue
        for step in (1, 10):
            row = closest_result_at(list(step_results[step]), time)
            if row is None:
                continue
            for key in ("S", "V", "P", "F"):
                maximum = max(
                    maximum,
                    abs(float(row.get(key, 0.0)) - float(reference.get(key, 0.0))),
                )
    return maximum


def _mean_absolute_trajectory_difference(
    left: Sequence[Dict[str, Any]],
    right: Sequence[Dict[str, Any]],
) -> float:
    if not left or not right:
        return 0.0
    differences = []
    for row in left:
        other = closest_result_at(list(right), str(row.get("time", "00:00")))
        if other is not None:
            differences.append(abs(float(row.get("S", 0.0)) - float(other.get("S", 0.0))))
    return sum(differences) / len(differences) if differences else 0.0


def _post_event_mean(sample: Mapping[str, Any], results: Sequence[Dict[str, Any]]) -> float:
    ends = [
        time_to_minutes(str(event.get("end_time") or event.get("end") or "00:00")[-5:])
        for event in sample.get("events", [])
        if isinstance(event, Mapping)
    ]
    if not ends:
        return 0.0
    start = max(ends)
    values = [
        float(row.get("S", 0.0))
        for row in results
        if start <= time_to_minutes(str(row.get("time", "00:00"))) <= start + 120
    ]
    return sum(values) / len(values) if values else 0.0


def _peak_stress(results: Sequence[Dict[str, Any]]) -> float:
    return max((float(row.get("S", 0.0)) for row in results), default=0.0)


def _series_summary(values: Sequence[float]) -> Dict[str, Any]:
    if not values:
        return {"mean": None, "variance": None, "lag1_autocorrelation": None, "peak": None}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    autocorrelation = None
    if len(values) >= 3 and variance > 1e-12:
        covariance = sum(
            (values[index] - mean) * (values[index - 1] - mean)
            for index in range(1, len(values))
        ) / (len(values) - 1)
        autocorrelation = covariance / variance
    return {
        "mean": mean,
        "variance": variance,
        "lag1_autocorrelation": autocorrelation,
        "peak": max(values),
    }


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def _minutes_to_time(value: int) -> str:
    value %= 1440
    return f"{value // 60:02d}:{value % 60:02d}"
