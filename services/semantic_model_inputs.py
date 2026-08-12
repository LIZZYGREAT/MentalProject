"""Canonical projection of semantic metadata consumed by prediction."""

from __future__ import annotations

from typing import Any, Mapping


OBJECTIVE_DIMENSIONS = (
    "difficulty",
    "cognitive_demand",
    "stakes",
    "time_pressure",
    "social_evaluation",
    "uncontrollability",
    "novelty",
    "expected_effort",
    "uncertainty",
    "unfinished",
)


def _unit(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _appraisal(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 5.0
    return max(1.0, min(10.0, number))


def semantic_model_inputs(semantic: Mapping[str, Any] | None) -> dict[str, float]:
    """Return only values that can change the dynamic-state calculation.

    Appraisal is projected to the exact F_like scale consumed by the model.
    Audit-only fields such as reasoning, tags, provider, and source are omitted.
    """

    semantic = semantic if isinstance(semantic, Mapping) else {}
    fused = semantic.get("fused") if isinstance(semantic.get("fused"), Mapping) else {}
    values = semantic.get("values")
    if not isinstance(values, Mapping):
        values = fused.get("objective_semantics")
    values = values if isinstance(values, Mapping) else {}
    appraisal = _appraisal(fused.get("appraisal_score_1_10", 5.0))
    return {
        **{key: _unit(values.get(key, 0.0)) for key in OBJECTIVE_DIMENSIONS},
        "appraisal_f_like": max(-1.0, min(1.0, (appraisal - 5.0) / 5.0)),
    }


def fused_appraisal_score(semantic: Mapping[str, Any] | None) -> float | None:
    """Return a valid fused 1-10 appraisal, or None when not supplied."""

    if not isinstance(semantic, Mapping):
        return None
    fused = semantic.get("fused")
    if not isinstance(fused, Mapping) or fused.get("appraisal_score_1_10") is None:
        return None
    try:
        value = float(fused["appraisal_score_1_10"])
    except (TypeError, ValueError):
        return None
    return value if 1.0 <= value <= 10.0 else None
