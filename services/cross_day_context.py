"""Build bounded, auditable context from the immediately preceding local day."""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from typing import Any, Dict, Iterable, Mapping, Optional


CONTEXT_SCHEMA_VERSION = "cross_day_context.v1"
DONE_STATUSES = {"done", "completed", "complete", "finished", "已完成"}
OPEN_STATUSES = {
    "pending",
    "incomplete",
    "unfinished",
    "ongoing",
    "todo",
    "部分完成",
    "未完成",
    "进行中",
}
RECOVERY_TYPES = {"rest", "meal", "nap", "sleep", "gym"}


def _clamp(value: Any, lower: float = 0.0, upper: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = lower
    if number > 1.0 and number <= 10.0:
        number /= 10.0
    elif number > 10.0:
        number /= 100.0
    return max(lower, min(upper, number))


def _normalized_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _stress_band(value: Any) -> str:
    try:
        stress = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if stress >= 80.0:
        return "very_high"
    if stress >= 70.0:
        return "high"
    if stress >= 58.0:
        return "elevated"
    if stress >= 42.0:
        return "typical"
    return "low"


def _completion_overrides(feedback: Iterable[Mapping[str, Any]]) -> Dict[str, bool]:
    """Latest event-completion feedback wins for each id/name key."""

    overrides: Dict[str, bool] = {}
    for item in feedback:
        if item.get("feedback_type") != "event_completion":
            continue
        payload = item.get("payload")
        if not isinstance(payload, Mapping) or "completed" not in payload:
            continue
        completed = bool(payload.get("completed"))
        for raw_key in (payload.get("event_id"), payload.get("event_name")):
            key = _normalized_name(raw_key)
            if key and key not in overrides:
                overrides[key] = completed
    return overrides


def _profile_index(run: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    diagnostics = run.get("diagnostics")
    profiles = diagnostics.get("event_profiles", []) if isinstance(diagnostics, Mapping) else []
    index: Dict[str, Mapping[str, Any]] = {}
    for profile in profiles if isinstance(profiles, list) else []:
        if not isinstance(profile, Mapping):
            continue
        for raw_key in (profile.get("event_id"), profile.get("name")):
            key = _normalized_name(raw_key)
            if key:
                index[key] = profile
    return index


def _explicit_completion(item: Mapping[str, Any]) -> Optional[bool]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    status = str(
        item.get("status")
        or item.get("event_status")
        or metadata.get("status")
        or metadata.get("event_status")
        or ""
    ).strip().casefold()
    if status in DONE_STATUSES:
        return True
    if status in OPEN_STATUSES:
        return False
    if "completed" in item:
        return bool(item.get("completed"))
    if "completed" in metadata:
        return bool(metadata.get("completed"))
    objective = item.get("objective")
    if not isinstance(objective, Mapping):
        objective = metadata.get("objective")
    if isinstance(objective, Mapping) and "unfinished" in objective:
        return _clamp(objective.get("unfinished")) < 0.5
    if "unfinished" in item:
        return _clamp(item.get("unfinished")) < 0.5
    if "unfinished" in metadata:
        return _clamp(metadata.get("unfinished")) < 0.5
    return None


def _semantic_values(profile: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not isinstance(profile, Mapping):
        return {}
    assessment = profile.get("assessment")
    semantic = assessment.get("semantic") if isinstance(assessment, Mapping) else {}
    values = semantic.get("values") if isinstance(semantic, Mapping) else {}
    return values if isinstance(values, Mapping) else {}


def _task_from_input(
    item: Mapping[str, Any],
    *,
    run: Mapping[str, Any],
    profiles: Mapping[str, Mapping[str, Any]],
    overrides: Mapping[str, bool],
) -> Optional[Dict[str, Any]]:
    event_type = str(item.get("event_type") or item.get("type") or "task").lower()
    if event_type in RECOVERY_TYPES:
        return None
    name = str(item.get("summary") or item.get("name") or "").strip()
    if not name:
        return None
    event_id = str(item.get("id") or item.get("event_id") or "").strip()
    key_candidates = [_normalized_name(event_id), _normalized_name(name)]
    completion = next(
        (overrides[key] for key in key_candidates if key in overrides),
        _explicit_completion(item),
    )
    # Never infer a cross-day unfinished task from a title or LLM prior alone.
    if completion is not False:
        return None

    profile = next((profiles[key] for key in key_candidates if key in profiles), None)
    semantic = _semantic_values(profile)
    objective = (
        (profile.get("assessment") or {}).get("objective", {})
        if isinstance(profile, Mapping)
        else {}
    )
    unfinished = max(
        0.65,
        _clamp(objective.get("unfinished", 1.0))
        if isinstance(objective, Mapping)
        else 1.0,
    )
    time_pressure = _clamp(semantic.get("time_pressure", 0.35))
    stakes = _clamp(semantic.get("stakes", 0.35))
    strength = _clamp(0.35 + 0.35 * unfinished + 0.18 * time_pressure + 0.12 * stakes)
    return {
        "event_id": event_id or None,
        "event_name": name[:120],
        "source_date": str(run.get("local_date") or "")[:10],
        "source_prediction_run_id": run.get("prediction_run_id"),
        "age_days": 1,
        "carry_strength": round(strength, 6),
        "explicitly_unfinished": True,
        "time_pressure": round(time_pressure, 6),
        "stakes": round(stakes, 6),
        "source": "event_completion_or_objective",
    }


def _inherited_tasks(
    run: Mapping[str, Any],
    overrides: Mapping[str, bool],
    max_carry_days: int,
) -> list[Dict[str, Any]]:
    run_input = run.get("input")
    prior_context = run_input.get("cross_day_context") if isinstance(run_input, Mapping) else {}
    raw_tasks = prior_context.get("unfinished_tasks", []) if isinstance(prior_context, Mapping) else []
    result = []
    for raw in raw_tasks if isinstance(raw_tasks, list) else []:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("event_name") or "").strip()
        event_id = str(raw.get("event_id") or "").strip()
        completion = next(
            (
                overrides[key]
                for key in (_normalized_name(event_id), _normalized_name(name))
                if key in overrides
            ),
            None,
        )
        if completion is True:
            continue
        age_days = max(1, int(raw.get("age_days") or 1)) + 1
        if age_days > max_carry_days:
            continue
        strength = _clamp(raw.get("carry_strength", 0.5)) * 0.68
        if strength < 0.12:
            continue
        result.append(
            {
                **dict(raw),
                "age_days": age_days,
                "carry_strength": round(strength, 6),
                "source_prediction_run_id": run.get("prediction_run_id"),
                "source": "bounded_inherited_unfinished",
            }
        )
    return result


def build_automatic_cross_day_context(
    database: Any,
    user_id: int,
    target_date: str,
    *,
    max_carry_days: int = 3,
) -> Optional[Dict[str, Any]]:
    """Use only the immediately preceding date and preserve full provenance."""

    target = datetime.strptime(str(target_date), "%Y-%m-%d").date()
    source_date = (target - timedelta(days=1)).isoformat()
    run = database.latest_prediction_run_for_date(int(user_id), source_date)
    if not run:
        return None

    result = run.get("result") if isinstance(run.get("result"), Mapping) else {}
    overrides = _completion_overrides(run.get("feedback", []))
    profiles = _profile_index(run)
    run_input = run.get("input") if isinstance(run.get("input"), Mapping) else {}
    raw_events = []
    for key in ("events", "mock_events"):
        values = run_input.get(key, []) if isinstance(run_input, Mapping) else []
        if isinstance(values, list):
            raw_events.extend(item for item in values if isinstance(item, Mapping))

    tasks = [
        task
        for item in raw_events
        if (
            task := _task_from_input(
                item,
                run=run,
                profiles=profiles,
                overrides=overrides,
            )
        )
    ]
    tasks.extend(_inherited_tasks(run, overrides, max(1, int(max_carry_days))))

    deduplicated: Dict[str, Dict[str, Any]] = {}
    for task in tasks:
        key = _normalized_name(task.get("event_id") or task.get("event_name"))
        if not key:
            continue
        existing = deduplicated.get(key)
        if existing is None or float(task["carry_strength"]) > float(
            existing["carry_strength"]
        ):
            deduplicated[key] = task
    tasks = sorted(
        deduplicated.values(),
        key=lambda item: (-float(item["carry_strength"]), item["event_name"]),
    )[:8]

    remaining = 1.0
    for task in tasks:
        remaining *= 1.0 - 0.65 * _clamp(task.get("carry_strength"))
    unfinished_load = min(0.90, 1.0 - remaining)
    state = {
        "S_end": result.get("end_S"),
        "V_end": result.get("end_V", result.get("end_E")),
        "E_end": result.get("end_E", result.get("end_V")),
        "P_end": result.get("end_P", 0.0),
        "F_end": result.get("end_F", 0.0),
    }
    diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), Mapping) else {}
    trajectories = diagnostics.get("event_trajectory", []) if isinstance(diagnostics, Mapping) else []
    high_load_events = sorted(
        (
            item
            for item in trajectories if isinstance(item, Mapping)
            and (
                _clamp(item.get("stress_intensity")) >= 0.55
                or float(item.get("peak_change") or 0.0) >= 3.0
            )
        ),
        key=lambda item: (
            -_clamp(item.get("stress_intensity")),
            -float(item.get("peak_change") or 0.0),
        ),
    )
    prior_context = run_input.get("cross_day_context") if isinstance(run_input, Mapping) else {}
    chain_depth = (
        int(prior_context.get("chain_depth") or 0) + 1
        if isinstance(prior_context, Mapping)
        else 1
    )
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "target_date": str(target_date),
        "source_date": source_date,
        "source_prediction_run_id": run.get("prediction_run_id"),
        "previous_day_state": state,
        "previous_day_end_stress_band": _stress_band(state.get("S_end")),
        "previous_day_high_load_events": [
            {
                "event_name": str(item.get("name") or "")[:80],
                "stress_intensity": round(_clamp(item.get("stress_intensity")), 6),
                "peak_change": round(float(item.get("peak_change") or 0.0), 3),
            }
            for item in high_load_events[:5]
            if item.get("name")
        ],
        "unfinished_tasks": tasks,
        "unfinished_load": round(unfinished_load, 6),
        "chain_depth": chain_depth,
        "policy": {
            "exact_previous_date_only": True,
            "explicit_completion_required": True,
            "max_unconfirmed_carry_days": max(1, int(max_carry_days)),
            "inherited_daily_decay": 0.68,
            "not_a_measurement": True,
        },
    }


def semantic_context_from_cross_day(
    context: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(context, Mapping):
        return {}
    tasks = context.get("unfinished_tasks", [])
    high_load_events = context.get("previous_day_high_load_events", [])
    names = [
        str(item.get("event_name") or "")[:80]
        for item in tasks if isinstance(item, Mapping) and item.get("event_name")
    ] if isinstance(tasks, list) else []
    return {
        "source_date": context.get("source_date"),
        "previous_day_end_stress_band": context.get(
            "previous_day_end_stress_band"
        ),
        "unfinished_task_count": len(names),
        "unfinished_task_names": names[:5],
        "previous_day_high_load_event_names": [
            str(item.get("event_name") or "")[:80]
            for item in high_load_events[:5]
            if isinstance(item, Mapping) and item.get("event_name")
        ] if isinstance(high_load_events, list) else [],
        "explicit_unfinished": bool(names),
    }
