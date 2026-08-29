"""Theory-guided nested continuous-time latent-state dynamics.

The model is intentionally small enough to remain identifiable before a user
has accumulated dense EMA observations.  Calendar events provide priors for
event appraisal; explicit user appraisal fields always take precedence.

The candidate states are introduced incrementally rather than assumed valid:

* M0: ``stress`` only;
* M1: stress plus ``vitality``;
* M2: M1 plus ``perseverative_cognition``;
* M3: M2 plus ``recovery_debt``.

The implementation keeps a common state container for API compatibility, but
inactive states are not allowed to feed back into a simpler candidate.  Which
candidate should be used in production is an empirical model-selection result,
not a user-selectable personality label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Any, Dict, Iterable, Mapping, Optional

from algorithm.time_utils import interval_minutes, parse_datetime_on_date
from utils.description_score import convert_score_to_Flike, score_description
from services.semantic_model_inputs import fused_appraisal_score, semantic_model_inputs
from services.workload import (
    WorkloadEstimator,
    event_workload_contribution,
    saturating_union,
)


RECOVERY_TYPES = {"rest", "meal", "nap", "sleep"}
_WORKLOAD_ESTIMATOR = WorkloadEstimator()

MODEL_VARIANTS: Dict[str, Dict[str, Any]] = {
    "m0": {
        "canonical": "stress-ctssm.m0",
        "label": "M0 压力时变平衡",
        "states": ("S",),
    },
    "m1": {
        "canonical": "stress-vitality-ctssm.m1",
        "label": "M1 压力与主观活力",
        "states": ("S", "V"),
    },
    "m2": {
        "canonical": "stress-vitality-pc-ctssm.m2",
        "label": "M2 加入持续性认知代理",
        "states": ("S", "V", "P"),
    },
    "m3": {
        "canonical": "stress-vitality-pc-fatigue-ctssm.m3",
        "label": "M3 加入恢复债代理",
        "states": ("S", "V", "P", "F"),
    },
}


def normalize_model_variant(value: Any) -> str:
    """Return an M0--M3 key from a model version, alias, or missing value.

    M0 is deliberately the fallback: the paper requires evidence before more
    latent states are retained.
    """

    text = str(value or "m0").strip().lower().replace("_", "-")
    for key in ("m3", "m2", "m1", "m0"):
        if text == key or f".{key}" in text or text.endswith(f"-{key}"):
            return key
    return "m0"


def model_variant_metadata(value: Any) -> Dict[str, Any]:
    key = normalize_model_variant(value)
    item = MODEL_VARIANTS[key]
    return {
        "key": key,
        "canonical": item["canonical"],
        "label": item["label"],
        "active_states": list(item["states"]),
        "candidate_status": "requires_out_of_time_validation",
    }


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def _unit(value: Any, default: float) -> float:
    """Normalize common 0-1, 0-10, or 0-100 inputs into [0, 1]."""

    if value is None or value == "":
        return _clamp(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _clamp(default)
    if number > 10.0:
        number /= 100.0
    elif number > 1.0:
        number /= 10.0
    return _clamp(number)


def _nested(metadata: Mapping[str, Any], group: str, key: str) -> Any:
    nested = metadata.get(group, {})
    if isinstance(nested, Mapping) and key in nested:
        return nested[key]
    return metadata.get(key)


def _combine(values: Iterable[float]) -> float:
    """Saturating union: overlapping inputs grow without linear explosion."""

    remainder = 1.0
    found = False
    for raw in values:
        found = True
        remainder *= 1.0 - _clamp(raw)
    return 1.0 - remainder if found else 0.0


@dataclass(frozen=True)
class LatentState:
    stress: float
    vitality: float
    perseverative_cognition: float = 0.0
    recovery_debt: float = 0.0


@dataclass(frozen=True)
class LatentUncertainty:
    """Approximate filtering variance on each display-scale latent state."""

    stress_variance: float = 100.0
    vitality_variance: float = 100.0
    cognition_variance: float = 0.04
    recovery_debt_variance: float = 0.04


@dataclass(frozen=True)
class EventAssessment:
    event_id: str
    event_type: str
    stress_intensity: float
    task_demand: float
    recovery_quality: float
    pre_weight: float
    post_weight: float
    onset_tau_minutes: float
    pre_tau_minutes: float
    post_tau_minutes: float
    onset_floor: float
    objective: Dict[str, float]
    appraisal: Dict[str, float]
    semantic: Dict[str, Any]
    workload_prior: float = 0.0
    workload_feature_vector: Dict[str, float] | None = None
    appraisal_observed: bool = False
    cancelled: bool = False
    cancelled_at: Optional[str] = None
    kernel_mode: str = "piecewise"


@dataclass(frozen=True)
class DynamicInputs:
    event_stress: float = 0.0
    task_demand: float = 0.0
    recovery: float = 0.0
    anticipatory_input: float = 0.0
    post_event_input: float = 0.0
    workload_raw: float = 0.0
    active_event_ids: tuple[str, ...] = ()
    active_event_names: tuple[str, ...] = ()


def assess_event(event: Any) -> EventAssessment:
    """Build objective attributes and appraisal priors for one event."""

    event_type = str(event.get_event_type()).lower()
    metadata = event.metadata if isinstance(getattr(event, "metadata", None), dict) else {}
    task_type = str(getattr(event, "task_type", metadata.get("task_type", "general"))).lower()
    duration = interval_minutes(event.start_time, event.end_time, default=60.0)
    duration_load = _clamp(duration / 180.0)
    semantic = metadata.get("semantic")
    if not isinstance(semantic, Mapping):
        # Prediction is deliberately network/database free.  Callers are
        # expected to inject semantics; this conservative neutral fallback
        # keeps older direct algorithm integrations working.
        semantic = {
            "schema_version": "event_semantics.fallback.v1",
            "source": "rules_fallback",
            "values": {
                "difficulty": 0.45,
                "cognitive_demand": 0.50,
                "physical_demand": 0.08,
                "stakes": 0.30,
                "time_pressure": 0.28,
                "social_evaluation": 0.18,
                "uncontrollability": 0.25,
                "novelty": 0.30,
                "expected_effort": 0.52,
                "uncertainty": 0.32,
                "unfinished": 0.22,
            },
        }
    model_semantics = semantic_model_inputs(semantic)
    semantic_values = {
        key: model_semantics[key]
        for key in (
            "difficulty", "cognitive_demand", "physical_demand", "stakes", "time_pressure",
            "social_evaluation", "uncontrollability", "novelty",
            "expected_effort", "uncertainty", "unfinished",
        )
    }

    objective_defaults: Dict[str, Dict[str, float]] = {
        "course": {
            "deadline": 0.15,
            "social_evaluation": 0.35,
            "uncontrollability": 0.35,
            "cognitive_demand": 0.68,
            "physical_demand": 0.05,
            "novelty": 0.25,
            "unfinished": 0.10,
        },
        "library": {
            "deadline": 0.25,
            "social_evaluation": 0.05,
            "uncontrollability": 0.20,
            "cognitive_demand": 0.72,
            "physical_demand": 0.05,
            "novelty": 0.12,
            "unfinished": 0.20,
        },
        "gym": {
            "deadline": 0.02,
            "social_evaluation": 0.10,
            "uncontrollability": 0.12,
            "cognitive_demand": 0.10,
            "physical_demand": float(getattr(event, "intensity", 0.65)),
            "novelty": 0.10,
            "unfinished": 0.02,
        },
        "rest": {
            "deadline": 0.0,
            "social_evaluation": 0.0,
            "uncontrollability": 0.05,
            "cognitive_demand": 0.02,
            "physical_demand": 0.02,
            "novelty": 0.02,
            "unfinished": 0.0,
        },
        "meal": {
            "deadline": 0.0,
            "social_evaluation": 0.02,
            "uncontrollability": 0.05,
            "cognitive_demand": 0.02,
            "physical_demand": 0.05,
            "novelty": 0.02,
            "unfinished": 0.0,
        },
        "nap": {
            "deadline": 0.0,
            "social_evaluation": 0.0,
            "uncontrollability": 0.02,
            "cognitive_demand": 0.0,
            "physical_demand": 0.0,
            "novelty": 0.0,
            "unfinished": 0.0,
        },
        "sleep": {
            "deadline": 0.0,
            "social_evaluation": 0.0,
            "uncontrollability": 0.02,
            "cognitive_demand": 0.0,
            "physical_demand": 0.0,
            "novelty": 0.0,
            "unfinished": 0.0,
        },
    }
    task_defaults = {
        "exam": (0.95, 0.88, 0.68, 0.88, 0.18),
        "ddl": (0.92, 0.42, 0.55, 0.82, 0.75),
        "meeting": (0.35, 0.62, 0.35, 0.52, 0.12),
        "homework": (0.55, 0.12, 0.28, 0.68, 0.42),
        "general": (0.28, 0.18, 0.25, 0.48, 0.18),
    }
    if event_type == "task":
        deadline, social, uncontrollable, cognitive, unfinished = task_defaults.get(
            task_type, task_defaults["general"]
        )
        defaults = {
            "deadline": deadline,
            "social_evaluation": social,
            "uncontrollability": uncontrollable,
            "cognitive_demand": cognitive,
            "physical_demand": 0.08,
            "novelty": 0.30,
            "unfinished": unfinished,
        }
    else:
        defaults = objective_defaults.get(
            event_type,
            objective_defaults["course"],
        )

    # Rule/API semantics supply priors only.  The explicit objective fields
    # below still take precedence dimension by dimension.
    defaults = dict(defaults)
    defaults.update(
        {
            "deadline": semantic_values["time_pressure"],
            "social_evaluation": semantic_values["social_evaluation"],
            "uncontrollability": semantic_values["uncontrollability"],
            "cognitive_demand": semantic_values["cognitive_demand"],
            "novelty": semantic_values["novelty"],
            "unfinished": semantic_values["unfinished"],
        }
    )
    semantic_payload_values = semantic.get("values")
    if not isinstance(semantic_payload_values, Mapping):
        fused_values = semantic.get("fused")
        semantic_payload_values = (
            fused_values.get("objective_semantics")
            if isinstance(fused_values, Mapping)
            else {}
        )
    if isinstance(semantic_payload_values, Mapping) and "physical_demand" in semantic_payload_values:
        defaults["physical_demand"] = semantic_values["physical_demand"]

    objective = {
        key: _unit(_nested(metadata, "objective", key), value)
        for key, value in defaults.items()
    }
    # A directly supplied controllability score is easier for clients to use.
    explicit_control = _nested(metadata, "objective", "control")
    if explicit_control is not None:
        objective["uncontrollability"] = 1.0 - _unit(explicit_control, 0.5)
    objective["duration"] = duration_load

    # Completion is a post-event observation, not something the forecast can
    # know in advance.  Keep the semantic work-remaining prior for audit, but
    # make the primary pre-event trajectory assume completion.  Only explicit
    # incomplete/partial feedback may create unfinished carry-over.
    lifecycle = metadata.get("lifecycle") if isinstance(metadata.get("lifecycle"), Mapping) else {}
    lifecycle = dict(lifecycle)
    if lifecycle:
        lifecycle.setdefault("work_remaining_prior", objective["unfinished"])
        outcome_status = str(lifecycle.get("outcome_status") or "pending").lower()
        completion_policy = str(lifecycle.get("completion_policy") or "none").lower()
        if completion_policy == "none" or outcome_status in {
            "pending",
            "assumed_completed",
            "confirmed_completed",
            "completed",
            "done",
            "not_applicable",
        }:
            objective["unfinished"] = 0.0
        elif outcome_status in {
            "confirmed_incomplete",
            "incomplete",
            "partial",
            "rescheduled",
        }:
            objective["unfinished"] = max(0.65, objective["unfinished"])
        metadata["lifecycle"] = lifecycle

    description_score = fused_appraisal_score(semantic)
    if description_score is None:
        description_score = score_description(
            str(getattr(event, "description", "") or ""),
            str(getattr(event, "name", "") or ""),
        )
    valence = convert_score_to_Flike(description_score)
    default_threat = _clamp(
        0.08
        + 0.26 * objective["deadline"]
        + 0.24 * objective["social_evaluation"]
        + 0.22 * objective["uncontrollability"]
        + 0.10 * objective["novelty"]
        + 0.10 * semantic_values["stakes"]
        + 0.12 * max(0.0, -valence)
    )
    default_importance = _clamp(
        0.20
        + 0.32 * objective["deadline"]
        + 0.22 * objective["social_evaluation"]
        + 0.16 * objective["cognitive_demand"]
        + 0.14 * semantic_values["stakes"]
    )
    default_control = 1.0 - objective["uncontrollability"]
    default_challenge = _clamp(
        0.18
        + 0.32 * objective["cognitive_demand"]
        + 0.22 * max(0.0, valence)
        - 0.12 * default_threat
    )
    appraisal_defaults = {
        "threat": default_threat,
        "challenge": default_challenge,
        "control": default_control,
        "importance": default_importance,
        "uncertainty": _clamp(
            max(
                semantic_values["uncertainty"],
                0.18
                + 0.34 * objective["novelty"]
                + 0.24 * objective["uncontrollability"],
            )
        ),
        "expected_effort": _clamp(
            max(
                semantic_values["expected_effort"],
                0.10
                + 0.50 * objective["cognitive_demand"]
                + 0.28 * objective["physical_demand"]
                + 0.10 * duration_load,
            )
        ),
        "rumination": objective["unfinished"] * 0.50,
    }
    raw_appraisal = metadata.get("appraisal")
    appraisal_observed = bool(
        isinstance(raw_appraisal, Mapping)
        and any(value is not None for value in raw_appraisal.values())
    )
    appraisal = {
        key: _unit(_nested(metadata, "appraisal", key), value)
        for key, value in appraisal_defaults.items()
    }

    workload_semantics = {
        **semantic_values,
        "cognitive_demand": objective["cognitive_demand"],
        "physical_demand": objective["physical_demand"],
        "time_pressure": objective["deadline"],
        "expected_effort": appraisal["expected_effort"],
        "uncontrollability": objective["uncontrollability"],
        "uncertainty": appraisal["uncertainty"],
    }
    workload_estimate = _WORKLOAD_ESTIMATOR.estimate(workload_semantics)
    workload_prior = (
        0.0 if event_type in RECOVERY_TYPES else workload_estimate.workload_prior
    )

    if event_type in RECOVERY_TYPES:
        stress_intensity = 0.0
    else:
        stress_intensity = _clamp(
            0.03
            + 0.10 * objective["deadline"]
            + 0.11 * objective["social_evaluation"]
            + 0.09 * objective["uncontrollability"]
            + 0.07 * objective["cognitive_demand"]
            + 0.04 * objective["novelty"]
            + 0.06 * objective["unfinished"]
            + 0.22 * appraisal["threat"]
            + 0.09 * appraisal["importance"]
            + 0.06 * appraisal["uncertainty"]
            + 0.05 * appraisal["expected_effort"]
            + 0.12 * semantic_values["difficulty"]
            + 0.06 * semantic_values["stakes"]
            - 0.05 * appraisal["challenge"]
            - 0.05 * appraisal["control"]
            - 0.05 * max(0.0, valence)
        )

    task_demand = _clamp(
        0.02
        + 0.29 * objective["cognitive_demand"]
        + 0.25 * objective["physical_demand"]
        + 0.15 * duration_load
        + 0.23 * appraisal["expected_effort"]
        + 0.06 * appraisal["importance"]
        + 0.10 * semantic_values["difficulty"]
    )
    if event_type in {"nap", "sleep"}:
        task_demand = 0.0

    recovery_defaults = {
        "rest": (0.62, 0.68, 0.72, 0.25, 0.05),
        "meal": (0.42, 0.48, 0.48, 0.12, 0.12),
        "nap": (0.88, 0.90, 0.70, 0.08, 0.04),
        "sleep": (0.92, 0.94, 0.60, 0.05, 0.04),
        "gym": (0.38, 0.46, 0.62, 0.35, 0.08),
        "course": (0.02, 0.01, 0.08, 0.22, 0.05),
        "library": (0.10, 0.06, 0.48, 0.58, 0.06),
        "task": (0.02, 0.01, 0.10, 0.28, 0.08),
    }
    detach, relax, control, mastery, interrupted = recovery_defaults.get(
        event_type, recovery_defaults["task"]
    )
    recovery_experience = {
        "detach": _unit(_nested(metadata, "recovery", "detach"), detach),
        "relax": _unit(_nested(metadata, "recovery", "relax"), relax),
        "control": _unit(_nested(metadata, "recovery", "control"), control),
        "mastery": _unit(_nested(metadata, "recovery", "mastery"), mastery),
        "interrupted": _unit(_nested(metadata, "recovery", "interrupted"), interrupted),
    }
    recovery_quality = _clamp(
        0.34 * recovery_experience["detach"]
        + 0.30 * recovery_experience["relax"]
        + 0.22 * recovery_experience["control"]
        + 0.14 * recovery_experience["mastery"]
        - 0.35 * recovery_experience["interrupted"]
    )

    pre_weight = _clamp(
        stress_intensity
        * (
            0.36 * appraisal["threat"]
            + 0.25 * appraisal["importance"]
            + 0.20 * appraisal["uncertainty"]
            + 0.19 * objective["deadline"]
        )
    )
    post_weight = _clamp(
        stress_intensity
        * (
            0.43 * objective["unfinished"]
            + 0.22 * appraisal["uncertainty"]
            + 0.18 * appraisal["importance"]
            + 0.17 * appraisal["rumination"]
        )
    )
    onset_tau = max(5.0, 18.0 + 18.0 * (1.0 - appraisal["threat"]))
    pre_tau = max(15.0, 35.0 + 55.0 * appraisal["importance"])
    post_tau = max(
        20.0,
        35.0
        + 80.0 * objective["unfinished"]
        + 45.0 * appraisal["uncertainty"]
        + 40.0 * appraisal["rumination"],
    )
    onset_floor = _clamp(
        0.30
        + 0.22 * semantic_values["difficulty"]
        + 0.16 * appraisal["importance"]
        + 0.13 * appraisal["threat"]
        + 0.09 * semantic_values["time_pressure"],
        0.30,
        0.78,
    )

    status = str(metadata.get("status") or metadata.get("event_status") or "").lower()
    cancelled = bool(metadata.get("cancelled", False)) or status in {
        "cancelled",
        "canceled",
    }
    cancelled_at = metadata.get("cancelled_at") or metadata.get("canceled_at")
    kernel_mode = str(metadata.get("kernel_mode") or "piecewise").lower()
    if kernel_mode not in {"piecewise", "exponential"}:
        kernel_mode = "piecewise"

    return EventAssessment(
        event_id=str(event.event_id),
        event_type=event_type,
        stress_intensity=stress_intensity,
        task_demand=task_demand,
        recovery_quality=recovery_quality,
        pre_weight=pre_weight,
        post_weight=post_weight,
        onset_tau_minutes=onset_tau,
        pre_tau_minutes=pre_tau,
        post_tau_minutes=post_tau,
        onset_floor=onset_floor,
        objective=objective,
        appraisal=appraisal,
        semantic=semantic,
        workload_prior=workload_prior,
        workload_feature_vector=workload_estimate.feature_vector,
        appraisal_observed=appraisal_observed,
        cancelled=cancelled,
        cancelled_at=str(cancelled_at) if cancelled_at else None,
        kernel_mode=kernel_mode,
    )


def build_event_assessments(events: Iterable[Any]) -> Dict[str, EventAssessment]:
    return {str(event.event_id): assess_event(event) for event in events}


def calculate_dynamic_inputs(
    events: Iterable[Any],
    assessments: Mapping[str, EventAssessment],
    current_time: datetime,
    date_str: str,
    sleep_appraisal_shift: float = 0.0,
) -> DynamicInputs:
    """Calculate event-onset, anticipation, aftermath, demand and recovery."""

    stress_inputs = []
    demands = []
    recoveries = []
    anticipatory = []
    post_event = []
    workload_inputs = []
    active_ids = []
    active_names = []

    for event in events:
        assessment = assessments[str(event.event_id)]
        appraisal_multiplier = (
            1.0
            if assessment.appraisal_observed
            else max(0.75, min(1.25, 1.0 - float(sleep_appraisal_shift)))
        )
        try:
            start = parse_datetime_on_date(event.start_time, date_str)
            end = parse_datetime_on_date(event.end_time, date_str)
            if end <= start:
                end += timedelta(days=1)
        except (TypeError, ValueError):
            continue

        cancellation_time = None
        if assessment.cancelled_at:
            try:
                cancellation_time = parse_datetime_on_date(
                    assessment.cancelled_at,
                    date_str,
                )
            except (TypeError, ValueError):
                cancellation_time = None

        # A fully cancelled event contributes no active-event peak.  When a
        # cancellation timestamp is known, earlier anticipation is preserved
        # and then allowed to leave only a short residual in P.
        if assessment.cancelled and cancellation_time is None:
            continue

        before_min = (start - current_time).total_seconds() / 60.0
        after_min = (current_time - end).total_seconds() / 60.0
        is_cancelled_now = bool(
            assessment.cancelled
            and cancellation_time is not None
            and current_time >= cancellation_time
        )
        is_active = start <= current_time < end and not is_cancelled_now

        if is_cancelled_now and cancellation_time is not None:
            since_cancel = max(
                0.0,
                (current_time - cancellation_time).total_seconds() / 60.0,
            )
            if since_cancel <= 90.0 and assessment.pre_weight > 0.0:
                post_event.append(
                    0.25 * assessment.pre_weight * math.exp(-since_cancel / 20.0)
                )
            continue

        workload_value, _ = event_workload_contribution(
            assessment.workload_prior,
            minutes_before_start=before_min,
            minutes_after_end=after_min,
            active=is_active,
        )
        if workload_value > 0.0:
            workload_inputs.append(workload_value)

        if 0.0 < before_min <= 240.0 and assessment.pre_weight > 0.0:
            anticipatory.append(
                assessment.pre_weight
                * appraisal_multiplier
                * _kernel_weight(
                    assessment.kernel_mode,
                    "pre",
                    before_min,
                    assessment.pre_tau_minutes,
                )
            )

        if is_active:
            active_ids.append(str(event.event_id))
            active_names.append(str(getattr(event, "name", "") or "未命名事件"))
            elapsed = max(0.0, (current_time - start).total_seconds() / 60.0)
            onset = _kernel_weight(
                assessment.kernel_mode,
                "active",
                elapsed,
                assessment.onset_tau_minutes,
                active_floor=assessment.onset_floor,
            )
            stress_inputs.append(
                assessment.stress_intensity * appraisal_multiplier * onset
            )
            demands.append(assessment.task_demand)
            recoveries.append(assessment.recovery_quality)

        if 0.0 <= after_min <= 360.0 and assessment.post_weight > 0.0:
            post_event.append(
                assessment.post_weight
                * appraisal_multiplier
                * _kernel_weight(
                    assessment.kernel_mode,
                    "post",
                    after_min,
                    assessment.post_tau_minutes,
                )
            )

    return DynamicInputs(
        event_stress=_combine(stress_inputs),
        task_demand=_combine(demands),
        recovery=_combine(recoveries),
        anticipatory_input=_combine(anticipatory),
        post_event_input=_combine(post_event),
        workload_raw=saturating_union(workload_inputs),
        active_event_ids=tuple(active_ids),
        active_event_names=tuple(active_names),
    )


def _piecewise_offset(hour: float, points: Iterable[tuple[float, float]]) -> float:
    ordered = sorted((float(x), float(y)) for x, y in points)
    if not ordered:
        return 0.0
    if hour <= ordered[0][0]:
        return ordered[0][1]
    if hour >= ordered[-1][0]:
        return ordered[-1][1]
    for (left_h, left_y), (right_h, right_y) in zip(ordered, ordered[1:]):
        if left_h <= hour <= right_h:
            ratio = (hour - left_h) / max(1e-9, right_h - left_h)
            return left_y + ratio * (right_y - left_y)
    return 0.0


def _interpolate_profile(value: float, points: Iterable[tuple[float, float]]) -> float:
    ordered = sorted((float(x), float(y)) for x, y in points)
    if not ordered:
        return 0.0
    if value <= ordered[0][0]:
        return ordered[0][1]
    if value >= ordered[-1][0]:
        return ordered[-1][1]
    for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:]):
        if left_x <= value <= right_x:
            ratio = (value - left_x) / max(1e-9, right_x - left_x)
            return left_y + ratio * (right_y - left_y)
    return 0.0


def _kernel_weight(
    mode: str,
    stage: str,
    minutes: float,
    tau: float,
    active_floor: float = 0.30,
) -> float:
    """Evaluate either a flexible piecewise basis or parsimonious exponential.

    Piecewise values are configurable engineering priors.  They must not be
    interpreted as empirically discovered shapes until fitted on EMA data.
    """

    value = max(0.0, float(minutes))
    floor = _clamp(active_floor, 0.20, 0.85)
    if mode == "exponential":
        if stage == "active":
            return floor + (1.0 - floor) * (
                1.0 - math.exp(-value / max(1e-6, tau))
            )
        return math.exp(-value / max(1e-6, tau))
    if stage == "pre":
        return _clamp(
            _interpolate_profile(
                value,
                ((0, 1.0), (30, 0.72), (60, 0.48), (120, 0.18), (240, 0.0)),
            )
        )
    if stage == "active":
        return max(
            floor,
            _clamp(
                _interpolate_profile(
                value,
                ((0, 0.30), (15, 0.55), (45, 0.82), (120, 0.97), (240, 1.0)),
                )
            ),
        )
    return _clamp(
        _interpolate_profile(
            value,
            ((0, 1.0), (30, 0.72), (60, 0.46), (120, 0.20), (240, 0.04), (360, 0.0)),
        )
    )


def step_latent_state(
    state: LatentState,
    inputs: DynamicInputs,
    *,
    current_time: datetime,
    dt_minutes: float,
    stress_baseline: float,
    sleep_debt_hours: float,
    config: Mapping[str, Any],
    sleeping: bool = False,
    model_variant: Any = "m0",
) -> tuple[LatentState, Dict[str, Any]]:
    """Advance one nested candidate with stable constant-input steps."""

    dt_hours = max(1e-6, float(dt_minutes) / 60.0)
    cfg = dict(config)
    variant = normalize_model_variant(model_variant)
    rank = int(variant[-1])
    use_vitality = rank >= 1
    use_cognition = rank >= 2
    use_fatigue = rank >= 3

    cognition_decay = max(
        0.05,
        float(cfg.get("cognition_decay_per_hour", 1.05)),
    )
    if use_cognition:
        cognition_drive = (
            float(cfg.get("anticipation_gain_per_hour", 0.90))
            * inputs.anticipatory_input
            + float(cfg.get("aftermath_gain_per_hour", 1.00))
            * inputs.post_event_input
        )
        cognition_eq = cognition_drive / cognition_decay
        cognition = cognition_eq + (
            state.perseverative_cognition - cognition_eq
        ) * math.exp(-cognition_decay * dt_hours)
        cognition = _clamp(cognition)
    else:
        cognition_drive = 0.0
        cognition = 0.0

    if use_fatigue:
        accumulation = (
            max(0.0, float(cfg.get("fatigue_accumulation_per_hour", 0.42)))
            * inputs.task_demand
        )
        restoration = (
            max(0.0, float(cfg.get("fatigue_recovery_per_hour", 0.95)))
            * inputs.recovery
        )
        fatigue_rate = accumulation + restoration
        if fatigue_rate > 1e-9:
            fatigue_eq = accumulation / fatigue_rate
            recovery_debt = fatigue_eq + (
                state.recovery_debt - fatigue_eq
            ) * math.exp(-fatigue_rate * dt_hours)
        else:
            recovery_debt = state.recovery_debt
        recovery_debt = _clamp(recovery_debt)
    else:
        accumulation = 0.0
        restoration = 0.0
        fatigue_rate = 0.0
        recovery_debt = 0.0

    hour = current_time.hour + current_time.minute / 60.0
    stress_tod = _piecewise_offset(
        hour,
        cfg.get(
            "stress_time_of_day",
            [(0, -2), (7, -1), (10, 0), (14, 1.5), (18, 2), (22, 1), (24, -2)],
        ),
    )
    debt_term = 0.0 if sleeping else min(
        8.0,
        max(0.0, sleep_debt_hours)
        * float(cfg.get("sleep_debt_stress_per_hour", 1.2)),
    )
    stress_equilibrium = (
        float(stress_baseline)
        + stress_tod
        + debt_term
        + float(cfg.get("event_stress_gain", 30.0)) * inputs.event_stress
    )
    # M0 has no latent perseverative-cognition state, but the paper's
    # time-varying equilibrium may still contain observable event covariates.
    # Retaining small, explicit pre/post kernels prevents a new demanding event
    # from pretending that the previous event's load vanished instantly.
    if not use_cognition:
        stress_equilibrium += (
            float(cfg.get("m0_anticipation_stress_gain", 5.0))
            * inputs.anticipatory_input
            + float(cfg.get("m0_post_event_stress_gain", 8.0))
            * inputs.post_event_input
        )
    if use_cognition:
        stress_equilibrium += (
            float(cfg.get("cognition_stress_gain", 15.0)) * cognition
        )
    if use_fatigue:
        stress_equilibrium += (
            float(cfg.get("fatigue_stress_gain", 17.0)) * recovery_debt
        )
    coupling = str(cfg.get("stress_vitality_coupling", "none")).lower()
    vitality_baseline = float(cfg.get("vitality_baseline", 72.0))
    if use_vitality and coupling in {"v_to_s", "bidirectional"}:
        stress_equilibrium -= float(
            cfg.get("vitality_to_stress_gain", 0.10)
        ) * (state.vitality - vitality_baseline)
    stress_equilibrium = max(5.0, min(95.0, stress_equilibrium))
    stress_rate = float(
        cfg.get(
            "stress_reactivity_per_hour"
            if stress_equilibrium >= state.stress
            else "stress_recovery_per_hour",
            1.55 if stress_equilibrium >= state.stress else 0.68,
        )
    )
    stress_rate = max(0.05, stress_rate)
    stress = stress_equilibrium + (
        state.stress - stress_equilibrium
    ) * math.exp(-stress_rate * dt_hours)
    stress = max(0.0, min(100.0, stress))

    vitality_tod = _piecewise_offset(
        hour,
        cfg.get(
            "vitality_time_of_day",
            [(0, -8), (7, 1), (10, 5), (14, 1), (18, -2), (22, -7), (24, -9)],
        ),
    )
    if use_vitality:
        vitality_equilibrium = (
            vitality_baseline
            + vitality_tod
            - min(
                10.0,
                max(0.0, sleep_debt_hours)
                * float(cfg.get("sleep_debt_vitality_per_hour", 1.8)),
            )
        )
        if use_fatigue:
            vitality_equilibrium -= (
                float(cfg.get("fatigue_vitality_gain", 27.0)) * recovery_debt
            )
        vitality_rate = max(
            0.05,
            float(cfg.get("vitality_regulation_per_hour", 0.58)),
        )
        vitality_equilibrium += (
            -float(cfg.get("demand_vitality_drain_per_hour", 13.0))
            * inputs.task_demand
            + float(cfg.get("recovery_vitality_gain_per_hour", 10.0))
            * inputs.recovery
        ) / vitality_rate
        if coupling in {"s_to_v", "bidirectional"}:
            vitality_equilibrium -= float(
                cfg.get("stress_to_vitality_gain", 0.08)
            ) * max(0.0, state.stress - float(stress_baseline))
        vitality_equilibrium = max(5.0, min(98.0, vitality_equilibrium))
        vitality = vitality_equilibrium + (
            state.vitality - vitality_equilibrium
        ) * math.exp(-vitality_rate * dt_hours)
        vitality = max(0.0, min(100.0, vitality))
    else:
        vitality_rate = 0.0
        vitality_equilibrium = vitality_baseline
        vitality = vitality_baseline

    new_state = LatentState(
        stress=stress,
        vitality=vitality,
        perseverative_cognition=cognition,
        recovery_debt=recovery_debt,
    )
    diagnostics = {
        "stress_equilibrium": stress_equilibrium,
        "vitality_equilibrium": vitality_equilibrium,
        "stress_time_of_day": stress_tod,
        "vitality_time_of_day": vitality_tod,
        "sleep_debt_effect": debt_term,
        "model_variant": variant,
        "active_states": list(MODEL_VARIANTS[variant]["states"]),
        "cognition_drive": cognition_drive,
        "fatigue_accumulation": accumulation,
        "fatigue_restoration": restoration,
        "stress_rate": stress_rate,
        "vitality_rate": vitality_rate,
    }
    return new_state, diagnostics


def initialize_latent_state(
    *,
    stress_baseline: float,
    vitality_baseline: float,
    previous: Optional[LatentState] = None,
    sleep_quality_deviation: float = 0.0,
    config: Optional[Mapping[str, Any]] = None,
    model_variant: Any = "m0",
) -> LatentState:
    """Apply the paper's centered cross-day transition for a new local day."""

    cfg = dict(config or {})
    variant = normalize_model_variant(model_variant)
    rank = int(variant[-1])
    sleep_delta = max(-1.0, min(1.0, float(sleep_quality_deviation)))
    if previous is None:
        return LatentState(
            stress=max(0.0, min(100.0, float(stress_baseline))),
            vitality=(
                max(0.0, min(100.0, float(vitality_baseline)))
                if rank >= 1
                else float(vitality_baseline)
            ),
            perseverative_cognition=0.0,
            recovery_debt=0.0,
        )

    stress = (
        float(stress_baseline)
        + float(cfg.get("cross_day_stress_persistence", 0.42))
        * (previous.stress - float(stress_baseline))
        + (
            float(cfg.get("cross_day_fatigue_stress_gain", 6.0))
            * previous.recovery_debt
            if rank >= 3
            else 0.0
        )
        - float(cfg.get("sleep_quality_initial_stress_gain", 5.0))
        * sleep_delta
    )
    vitality = (
        float(vitality_baseline)
        + float(cfg.get("cross_day_vitality_persistence", 0.38))
        * (previous.vitality - float(vitality_baseline))
        + float(cfg.get("sleep_quality_initial_vitality_gain", 7.0))
        * sleep_delta
    )
    return LatentState(
        stress=max(0.0, min(100.0, stress)),
        vitality=max(0.0, min(100.0, vitality)),
        perseverative_cognition=(
            _clamp(
                previous.perseverative_cognition
                * float(cfg.get("cross_day_cognition_persistence", 0.15))
            )
            if rank >= 2
            else 0.0
        ),
        recovery_debt=(
            _clamp(
                previous.recovery_debt
                * float(cfg.get("cross_day_fatigue_persistence", 0.62))
            )
            if rank >= 3
            else 0.0
        ),
    )


def initialize_uncertainty(
    config: Optional[Mapping[str, Any]] = None,
    model_variant: Any = "m0",
) -> LatentUncertainty:
    cfg = dict(config or {})
    rank = int(normalize_model_variant(model_variant)[-1])
    return LatentUncertainty(
        stress_variance=float(cfg.get("initial_stress_variance", 100.0)),
        vitality_variance=(
            float(cfg.get("initial_vitality_variance", 100.0)) if rank >= 1 else 0.0
        ),
        cognition_variance=(
            float(cfg.get("initial_cognition_variance", 0.04)) if rank >= 2 else 0.0
        ),
        recovery_debt_variance=(
            float(cfg.get("initial_fatigue_variance", 0.04)) if rank >= 3 else 0.0
        ),
    )


def step_uncertainty(
    uncertainty: LatentUncertainty,
    *,
    diagnostics: Mapping[str, Any],
    dt_minutes: float,
    config: Mapping[str, Any],
    model_variant: Any,
) -> LatentUncertainty:
    """Propagate approximate OU/process variance for prediction intervals."""

    cfg = dict(config)
    dt_hours = max(1e-6, float(dt_minutes) / 60.0)
    rank = int(normalize_model_variant(model_variant)[-1])

    def advance(variance: float, rate: float, process_sd: float) -> float:
        rate = max(1e-6, float(rate))
        decay = math.exp(-2.0 * rate * dt_hours)
        innovation = (process_sd**2) * (1.0 - decay) / (2.0 * rate)
        return max(1e-9, decay * max(0.0, variance) + innovation)

    return LatentUncertainty(
        stress_variance=advance(
            uncertainty.stress_variance,
            float(diagnostics.get("stress_rate", 1.0)),
            float(cfg.get("stress_process_sd_per_sqrt_hour", 3.0)),
        ),
        vitality_variance=(
            advance(
                uncertainty.vitality_variance,
                float(diagnostics.get("vitality_rate", 0.58)),
                float(cfg.get("vitality_process_sd_per_sqrt_hour", 3.5)),
            )
            if rank >= 1
            else 0.0
        ),
        cognition_variance=(
            advance(
                uncertainty.cognition_variance,
                float(cfg.get("cognition_decay_per_hour", 1.05)),
                float(cfg.get("cognition_process_sd_per_sqrt_hour", 0.08)),
            )
            if rank >= 2
            else 0.0
        ),
        recovery_debt_variance=(
            advance(
                uncertainty.recovery_debt_variance,
                max(
                    0.05,
                    float(diagnostics.get("fatigue_accumulation", 0.0))
                    + float(diagnostics.get("fatigue_restoration", 0.0)),
                ),
                float(cfg.get("fatigue_process_sd_per_sqrt_hour", 0.05)),
            )
            if rank >= 3
            else 0.0
        ),
    )


def assimilate_observation(
    state: LatentState,
    observation: Mapping[str, Any],
) -> LatentState:
    """Compatibility wrapper for one uncertainty-aware EMA state update."""

    updated, _ = assimilate_observation_with_uncertainty(
        state,
        initialize_uncertainty({}, "m3"),
        observation,
        config={},
        model_variant="m3",
    )
    return updated


def assimilate_observation_with_uncertainty(
    state: LatentState,
    uncertainty: LatentUncertainty,
    observation: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    model_variant: Any,
) -> tuple[LatentState, LatentUncertainty]:
    """Apply a scalar Kalman-style update with recall-dependent noise.

    This is an online state correction only.  It never permanently rewrites a
    user's reaction or recovery parameters from a single report.
    """

    cfg = dict(config)
    rank = int(normalize_model_variant(model_variant)[-1])
    retrospective = bool(observation.get("retrospective", False))
    delay_minutes = max(
        0.0,
        float(observation.get("recall_delay_minutes", 0.0) or 0.0),
    )
    delay_factor = (
        1.0
        + float(cfg.get("observation_delay_variance_per_hour", 0.55))
        * delay_minutes
        / 60.0
        + (
            float(cfg.get("retrospective_variance_multiplier", 0.75))
            if retrospective
            else 0.0
        )
    )

    def normalized_value(key: str, *, scale_100: bool) -> Optional[float]:
        raw = observation.get(key)
        if raw is None or raw == "":
            return None
        value = float(raw)
        if scale_100 and 0.0 <= value <= 10.0:
            value *= 10.0
        elif not scale_100 and value > 1.0:
            value /= 100.0 if value > 10.0 else 10.0
        return max(0.0, min(100.0 if scale_100 else 1.0, value))

    def update(
        current: float,
        variance: float,
        observed_value: Optional[float],
        base_sd: float,
    ) -> tuple[float, float]:
        if observed_value is None:
            return current, variance
        observation_variance = (max(1e-6, base_sd) ** 2) * delay_factor
        gain = max(
            0.02,
            min(0.85, variance / max(1e-9, variance + observation_variance)),
        )
        corrected = current + gain * (observed_value - current)
        corrected_variance = max(1e-9, (1.0 - gain) * variance)
        return corrected, corrected_variance

    stress, stress_variance = update(
        state.stress,
        uncertainty.stress_variance,
        normalized_value("stress", scale_100=True),
        float(cfg.get("stress_observation_sd", 8.0)),
    )
    vitality, vitality_variance = (
        update(
            state.vitality,
            uncertainty.vitality_variance,
            normalized_value("vitality", scale_100=True),
            float(cfg.get("vitality_observation_sd", 9.0)),
        )
        if rank >= 1
        else (state.vitality, 0.0)
    )
    cognition, cognition_variance = (
        update(
            state.perseverative_cognition,
            uncertainty.cognition_variance,
            normalized_value("perseverative_cognition", scale_100=False),
            float(cfg.get("cognition_observation_sd", 0.18)),
        )
        if rank >= 2
        else (0.0, 0.0)
    )
    return (
        LatentState(
            stress=max(0.0, min(100.0, stress)),
            vitality=max(0.0, min(100.0, vitality)),
            perseverative_cognition=_clamp(cognition),
            recovery_debt=state.recovery_debt,
        ),
        LatentUncertainty(
            stress_variance=stress_variance,
            vitality_variance=vitality_variance,
            cognition_variance=cognition_variance,
            recovery_debt_variance=uncertainty.recovery_debt_variance,
        ),
    )


def prediction_interval(
    value: float,
    variance: float,
    *,
    lower_bound: float,
    upper_bound: float,
    z: float = 1.6448536269514722,
) -> tuple[float, float]:
    """Return a normal-approximation 90% interval on the display scale."""

    margin = float(z) * math.sqrt(max(0.0, float(variance)))
    return (
        max(lower_bound, float(value) - margin),
        min(upper_bound, float(value) + margin),
    )


def stress_semantic_label(stress: float) -> str:
    if stress < 35.0:
        return "较低"
    if stress < 55.0:
        return "日常波动"
    if stress < 68.0:
        return "偏高"
    if stress < 80.0:
        return "高压"
    return "很高"


def vitality_semantic_label(vitality: float) -> str:
    if vitality < 25.0:
        return "很低"
    if vitality < 45.0:
        return "偏低"
    if vitality < 70.0:
        return "一般"
    return "充足"
