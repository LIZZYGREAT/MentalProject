"""Reproducible numerical checks for the intended psychological semantics."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Mapping, Sequence

from calibration.metrics import closest_result_at
from calibration.simulation_runner import run_simulation_for_calibration


def run_numerical_semantic_check(base_params: Mapping[str, Any]) -> Dict[str, Any]:
    """Exercise interpretable contrasts; this is not empirical validation."""
    calm = _run([], base_params, "m0")
    challenge = _run([_demand_event("challenge", threatened=False)], base_params, "m0")
    threat = _run([_demand_event("threat", threatened=True)], base_params, "m0")
    heavy = _run(_heavy_events(), base_params, "m0")
    recovery = _run(_recovery_events(), base_params, "m3")
    competition_day = _run(
        _competition_day_events(),
        base_params,
        "m0",
        date_str="2025-11-07",
        weave_routines=True,
    )
    course_day = _run(
        _course_day_events(),
        base_params,
        "m0",
        date_str="2026-08-01",
        weave_routines=True,
    )

    calm_peak = _peak(calm["results"])
    challenge_peak = _peak(challenge["results"])
    threat_peak = _peak(threat["results"])
    heavy_peak = _peak(heavy["results"])
    before = closest_result_at(recovery["results"], "08:00")
    loaded = closest_result_at(recovery["results"], "12:00")
    recovered = closest_result_at(recovery["results"], "13:00")
    competition_segments = [
        item
        for item in competition_day["event_trajectory"]
        if item["name"] == "数竞"
    ]
    course_active = [
        float(row["S"])
        for row in course_day["results"]
        if "08:00" <= row["time"] < "17:40"
    ]
    course_active_sorted = sorted(course_active)

    values = {
        "calm_m0": {
            "peak_stress": calm_peak,
            "care_count": len(calm["alerts"]),
        },
        "liked_high_demand_m0": {
            "peak_stress": challenge_peak,
            "care_count": len(challenge["alerts"]),
        },
        "high_threat_same_demand_m0": {
            "peak_stress": threat_peak,
            "care_count": len(threat["alerts"]),
        },
        "stacked_high_load_m0": {
            "peak_stress": heavy_peak,
            "care_count": len(heavy["alerts"]),
        },
        "m3_candidate_load_then_recovery": {
            "stress_08_00": float(before["S"]),
            "stress_12_00": float(loaded["S"]),
            "stress_13_00": float(recovered["S"]),
            "vitality_08_00": float(before["V"]),
            "vitality_12_00": float(loaded["V"]),
            "vitality_13_00": float(recovered["V"]),
            "recovery_debt_12_00": float(loaded["F"]),
            "recovery_debt_13_00": float(recovered["F"]),
        },
        "screenshot_competition_segments_m0": [
            {
                "time": item["time"],
                "stress_before": item["stress_before"],
                "stress_peak": item["stress_peak"],
                "peak_change": item["peak_change"],
                "end_change": item["end_change"],
                "slope_per_hour": item["slope_per_hour"],
                "status": item["status"],
            }
            for item in competition_segments
        ],
        "full_day_advanced_courses_m0": {
            "active_median_stress": (
                course_active_sorted[len(course_active_sorted) // 2]
                if course_active_sorted
                else None
            ),
            "active_peak_stress": max(course_active, default=0.0),
            "care_count": len(course_day["alerts"]),
        },
    }
    gates = {
        "calm_does_not_trigger_care": (
            calm_peak < 70.0 and len(calm["alerts"]) == 0
        ),
        "threat_appraisal_exceeds_liked_challenge": (
            threat_peak >= challenge_peak + 5.0
        ),
        "stacked_high_load_is_not_silent": (
            heavy_peak >= 70.0 and 1 <= len(heavy["alerts"]) <= 3
        ),
        "demand_lowers_vitality_and_builds_recovery_debt": (
            float(loaded["V"]) <= float(before["V"]) - 10.0
            and float(loaded["F"]) >= 0.30
        ),
        "rated_recovery_moves_all_candidate_states_in_expected_direction": (
            float(recovered["S"]) < float(loaded["S"])
            and float(recovered["V"]) > float(loaded["V"])
            and float(recovered["F"]) < float(loaded["F"])
        ),
        "math_competition_segments_have_positive_relative_peaks": (
            len(competition_segments) == 2
            and all(item["peak_change"] >= 3.0 for item in competition_segments)
            and all(item["status"] == "passed" for item in competition_segments)
        ),
        "full_course_day_is_moderate_without_care_spam": (
            bool(course_active_sorted)
            and course_active_sorted[len(course_active_sorted) // 2] >= 59.0
            and 63.0 <= max(course_active) < 75.0
            and len(course_day["alerts"]) == 0
        ),
    }
    return {
        "schema_version": "numerical_semantic_check.v1",
        "evidence_type": "engineering_sanity_check_not_empirical_validation",
        "values": values,
        "gates": gates,
        "passed": all(gates.values()),
    }


def _run(
    events: Sequence[Dict[str, Any]],
    base_params: Mapping[str, Any],
    variant: str,
    date_str: str = "2026-07-31",
    weave_routines: bool = False,
) -> Dict[str, Any]:
    params = deepcopy(dict(base_params))
    versions = {
        "m0": "stress-ctssm.m0",
        "m3": "stress-vitality-pc-fatigue-ctssm.m3",
    }
    params["model_family"] = versions[variant]
    params["model_selection"] = {
        **dict(params.get("model_selection", {}) or {}),
        "active_variant": variant,
        "status": "research_candidate_run",
    }
    return run_simulation_for_calibration(
        date_str,
        list(events),
        user_params=params,
        weave_routines=weave_routines,
    )


def _demand_event(event_id: str, *, threatened: bool) -> Dict[str, Any]:
    return {
        "id": event_id,
        "event_type": "task",
        "task_type": "general",
        "summary": "same high-demand task",
        "start_time": "09:00",
        "end_time": "12:00",
        "objective": {
            "cognitive_demand": 0.90,
            "physical_demand": 0.10,
            "deadline": 0.40,
            "social_evaluation": 0.30,
            "uncontrollability": 0.30,
        },
        "appraisal": (
            {
                "threat": 0.90,
                "challenge": 0.10,
                "control": 0.10,
                "importance": 0.90,
                "uncertainty": 0.90,
                "expected_effort": 0.90,
            }
            if threatened
            else {
                "threat": 0.10,
                "challenge": 0.90,
                "control": 0.90,
                "importance": 0.70,
                "uncertainty": 0.10,
                "expected_effort": 0.90,
            }
        ),
    }


def _heavy_events() -> list[Dict[str, Any]]:
    return [
        {
            "id": "exam",
            "event_type": "task",
            "task_type": "exam",
            "summary": "high-stakes evaluation",
            "start_time": "08:30",
            "end_time": "11:30",
            "appraisal": {
                "threat": 0.90,
                "importance": 1.00,
                "control": 0.20,
                "uncertainty": 0.80,
                "expected_effort": 0.95,
            },
        },
        {
            "id": "deadline",
            "event_type": "task",
            "task_type": "ddl",
            "summary": "unfinished deadline",
            "start_time": "13:00",
            "end_time": "18:00",
            "objective": {"deadline": 1.0, "unfinished": 1.0},
            "appraisal": {
                "threat": 0.95,
                "importance": 1.00,
                "control": 0.10,
                "uncertainty": 0.90,
                "expected_effort": 1.00,
                "rumination": 0.90,
            },
        },
    ]


def _recovery_events() -> list[Dict[str, Any]]:
    return [
        {
            **_demand_event("deadline-load", threatened=True),
            "task_type": "ddl",
            "objective": {
                "deadline": 1.0,
                "unfinished": 0.8,
                "cognitive_demand": 0.9,
            },
        },
        {
            "id": "rated-recovery",
            "event_type": "rest",
            "summary": "self-chosen restorative break",
            "start_time": "12:30",
            "end_time": "13:00",
            "recovery": {
                "detach": 0.9,
                "relax": 0.9,
                "control": 0.9,
                "mastery": 0.7,
            },
        },
    ]


def _competition_day_events() -> list[Dict[str, Any]]:
    return [
        {"summary": "离散数学", "start_time": "08:00", "end_time": "09:40"},
        {"summary": "数竞", "start_time": "10:00", "end_time": "11:40"},
        {"summary": "数竞", "start_time": "12:20", "end_time": "13:40"},
        {"summary": "心理项目", "start_time": "14:00", "end_time": "17:40"},
        {
            "summary": "算法/心理项目/比赛",
            "start_time": "18:30",
            "end_time": "23:00",
        },
    ]


def _course_day_events() -> list[Dict[str, Any]]:
    return [
        {
            "event_type": "course",
            "summary": name,
            "start_time": start,
            "end_time": end,
        }
        for name, (start, end) in zip(
            ("离散数学", "高等数学", "线性代数", "概率论"),
            (
                ("08:00", "10:00"),
                ("10:10", "12:10"),
                ("13:30", "15:30"),
                ("15:40", "17:40"),
            ),
        )
    ]


def _peak(results: Sequence[Mapping[str, Any]]) -> float:
    return max((float(row.get("S", 0.0)) for row in results), default=0.0)
