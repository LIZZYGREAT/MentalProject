"""Per-event trajectory diagnostics for semantic and numerical review."""

from __future__ import annotations

from statistics import median
from typing import Any, Dict, Iterable, Mapping


RECOVERY_TYPES = {"rest", "meal", "nap", "sleep"}


def _minute(value: Any) -> int:
    text = str(value or "00:00").split(" ")[-1][:5]
    hour, minute = text.split(":", 1)
    return int(hour) * 60 + int(minute)


def _slope_per_hour(values: list[float], step_minutes: float) -> float:
    if len(values) < 2:
        return 0.0
    center_x = (len(values) - 1) / 2.0
    center_y = sum(values) / len(values)
    denominator = sum((index - center_x) ** 2 for index in range(len(values)))
    if denominator <= 1e-9:
        return 0.0
    per_step = sum(
        (index - center_x) * (value - center_y)
        for index, value in enumerate(values)
    ) / denominator
    return per_step * 60.0 / max(1e-6, step_minutes)


def _trend_label(slope: float, peak_delta: float, end_delta: float) -> str:
    if abs(slope) <= 0.35 and abs(end_delta) <= 1.0:
        return "plateau"
    if slope >= 0.55 and end_delta >= 0.5:
        return "rising"
    if slope <= -0.55 and end_delta <= -0.5:
        return "declining"
    if peak_delta >= 1.0 and end_delta <= peak_delta - 1.0:
        return "rise_then_recover"
    return "mixed"


def build_event_trajectory_diagnostics(
    results: Iterable[Mapping[str, Any]],
    events: Iterable[Any],
    assessments: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Measure start/peak/end changes and compare them with event semantics.

    These are engineering acceptance checks, not empirical psychological
    validation.  A hard task is allowed to plateau or decline when explicit
    appraisal or a high entry state supports that interpretation; the function
    flags silent semantic inversions rather than enforcing monotonic growth.
    """

    rows = list(results)
    if not rows:
        return {}
    row_minutes = [_minute(row.get("time")) for row in rows]
    step_minutes = 5.0
    if len(row_minutes) >= 2:
        difference = (row_minutes[1] - row_minutes[0]) % (24 * 60)
        if difference:
            step_minutes = float(difference)

    output: Dict[str, Dict[str, Any]] = {}
    for event in events:
        event_id = str(event.event_id)
        assessment = assessments[event_id]
        start = _minute(event.start_time)
        end = _minute(event.end_time)
        if end <= start:
            end += 24 * 60

        segment_indexes = []
        for index, minute in enumerate(row_minutes):
            comparable = minute + (24 * 60 if minute < start and end > 24 * 60 else 0)
            if start <= comparable < end:
                segment_indexes.append(index)
        if not segment_indexes:
            continue

        first_index = segment_indexes[0]
        pre_index = max(0, first_index - 1)
        exit_indexes = []
        for index, minute in enumerate(row_minutes):
            comparable = minute + (24 * 60 if minute < start and end > 24 * 60 else 0)
            if comparable >= end:
                exit_indexes.append((comparable, index))
        exit_index = min(exit_indexes)[1] if exit_indexes else segment_indexes[-1]
        segment = [float(rows[index].get("S", 0.0)) for index in segment_indexes]
        equilibria = [
            float(rows[index].get("stress_equilibrium", segment[offset]))
            for offset, index in enumerate(segment_indexes)
        ]
        pre = float(rows[pre_index].get("S", segment[0]))
        start_stress = segment[0]
        peak = max(segment)
        trough = min(segment)
        in_event_last_stress = segment[-1]
        end_stress = float(rows[exit_index].get("S", in_event_last_stress))
        peak_index = segment.index(peak)
        peak_time = str(rows[segment_indexes[peak_index]].get("time", ""))
        peak_delta = peak - pre
        end_delta = end_stress - pre
        slope = _slope_per_hour(segment, step_minutes)
        trend = _trend_label(slope, peak_delta, end_delta)

        semantic = assessment.semantic or {}
        semantic_values = semantic.get("values", {})
        difficulty = float(semantic_values.get("difficulty", 0.0))
        high_activation = float(assessment.stress_intensity) >= 0.55
        high_demand = difficulty >= 0.75 or float(assessment.task_demand) >= 0.68
        recovery = assessment.event_type in RECOVERY_TYPES
        explicit_appraisal = bool(assessment.appraisal_observed)
        median_equilibrium = median(equilibria)

        if recovery:
            expected = "decline_or_plateau"
            warning = end_delta > 1.5 and slope > 0.5
            expectation_basis = "recovery_event"
        elif high_activation:
            expected = "rise_or_high_plateau"
            warning = peak_delta < -0.25 or (
                end_delta < -1.5 and slope < -0.55 and not explicit_appraisal
            )
            expectation_basis = "high_stress_input"
        elif high_demand:
            expected = "non_declining_or_context_explained"
            warning = peak_delta < -0.25 and median_equilibrium < pre - 0.5
            expectation_basis = "high_semantic_difficulty"
        else:
            expected = "context_dependent"
            warning = False
            expectation_basis = "moderate_or_low_activation"

        explanation = "trajectory_consistent"
        if warning:
            explanation = "semantic_trend_mismatch"
        elif end_delta < -0.5 and median_equilibrium < pre:
            explanation = "entered_above_segment_equilibrium"
        elif abs(end_delta) <= 1.0:
            explanation = "high_plateau_or_stable_segment"

        output[event_id] = {
            "schema_version": "event_trajectory_diagnostic.v1",
            "event_id": event_id,
            "name": str(getattr(event, "name", "") or "未命名事件"),
            "event_type": assessment.event_type,
            "time": (
                f"{start % (24 * 60) // 60:02d}:{start % 60:02d}-"
                f"{end % (24 * 60) // 60:02d}:{end % 60:02d}"
            ),
            "stress_before": round(pre, 3),
            "stress_start": round(start_stress, 3),
            "stress_peak": round(peak, 3),
            "peak_time": peak_time,
            "stress_end": round(end_stress, 3),
            "stress_in_event_last": round(in_event_last_stress, 3),
            "stress_trough": round(trough, 3),
            "peak_change": round(peak_delta, 3),
            "end_change": round(end_delta, 3),
            "relative_peak_change_percent": round(
                100.0 * peak_delta / max(1.0, abs(pre)),
                3,
            ),
            "slope_per_hour": round(slope, 3),
            "median_stress_equilibrium": round(median_equilibrium, 3),
            "trend": trend,
            "expected_trend": expected,
            "expectation_basis": expectation_basis,
            "explanation": explanation,
            "status": "warning" if warning else "passed",
            "semantic_difficulty": round(difficulty, 3),
            "stress_intensity": round(float(assessment.stress_intensity), 3),
            "task_demand": round(float(assessment.task_demand), 3),
            "explicit_appraisal": explicit_appraisal,
            "semantic_source": semantic.get("source", "rules"),
            "semantic_fingerprint": semantic.get("fingerprint"),
            "semantic_versions": {
                "rule": semantic.get("rule_version"),
                "prompt": semantic.get("prompt_version"),
                "fusion": semantic.get("fusion_policy_version"),
                "model": semantic.get("model"),
            },
            "matched_rules": semantic.get("matched_rules", []),
            "semantic_evidence_tags": semantic.get("evidence_tags", []),
            "semantic_reasoning_summary": semantic.get("reasoning_summary", ""),
        }
    return output
