"""Explainable workload features, priors, calibration, and time kernels.

Workload is deliberately a derived quantity rather than a latent CTSSM state.
All public scores use the closed [0, 1] interval so event concurrency can be
combined with a saturating union without changing scale.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


WORKLOAD_SCHEMA_VERSION = "event_workload.v1"
WORKLOAD_MODEL_VERSION = "workload-rules-logistic.v1"
WORKLOAD_FEATURE_NAMES = (
    "mental_demand",
    "physical_demand",
    "temporal_demand",
    "effort",
    "frustration",
)

# Rule-initialized coefficients.  They are intentionally modest and all
# positive: a typical medium-demand event maps near 0.5, while recovery events
# stay close to zero.  Event Appraisal data can replace them via ridge fitting.
DEFAULT_INTERCEPT = -2.0
DEFAULT_COEFFICIENTS = {
    "mental_demand": 0.95,
    "physical_demand": 0.55,
    "temporal_demand": 0.85,
    "effort": 0.85,
    "frustration": 0.80,
}


def _unit(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    if number > 10.0:
        number /= 100.0
    elif number > 1.0:
        number /= 10.0
    return max(0.0, min(1.0, number))


def _sigmoid(value: float) -> float:
    bounded = max(-40.0, min(40.0, float(value)))
    return 1.0 / (1.0 + math.exp(-bounded))


def saturating_union(values: Iterable[float]) -> float:
    """Combine concurrent loads as ``1 - product(1 - value)``."""

    remainder = 1.0
    found = False
    for value in values:
        found = True
        remainder *= 1.0 - _unit(value)
    return 1.0 - remainder if found else 0.0


def workload_feature_vector(
    semantic_values: Mapping[str, Any] | None,
    *,
    deadline_proximity: float = 0.0,
    user_frustration: float | None = None,
    perceived_control: float | None = None,
) -> dict[str, float]:
    """Map MindFlow semantic dimensions to NASA-TLX-style constructs."""

    values = semantic_values if isinstance(semantic_values, Mapping) else {}
    cognitive = _unit(values.get("cognitive_demand"))
    difficulty = _unit(values.get("difficulty"))
    uncontrollability = _unit(values.get("uncontrollability"))
    uncertainty = _unit(values.get("uncertainty"))
    frustration_parts = [uncontrollability, uncertainty]
    if user_frustration is not None:
        frustration_parts.append(_unit(user_frustration))
    if perceived_control is not None:
        frustration_parts.append(1.0 - _unit(perceived_control))
    return {
        "mental_demand": round(0.65 * cognitive + 0.35 * difficulty, 6),
        "physical_demand": round(_unit(values.get("physical_demand")), 6),
        "temporal_demand": round(
            max(_unit(values.get("time_pressure")), _unit(deadline_proximity)),
            6,
        ),
        "effort": round(_unit(values.get("expected_effort")), 6),
        "frustration": round(sum(frustration_parts) / len(frustration_parts), 6),
    }


@dataclass(frozen=True)
class WorkloadEstimate:
    schema_version: str
    model_version: str
    feature_vector: dict[str, float]
    workload_prior: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkloadFit:
    model_version: str
    sample_count: int
    ridge_alpha: float
    intercept: float
    coefficients: dict[str, float]
    mae: float
    rmse: float
    link: str = "identity_clip"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkloadEstimator:
    """Rule-initialized estimator with a small-sample ridge calibration path."""

    def __init__(
        self,
        *,
        intercept: float = DEFAULT_INTERCEPT,
        coefficients: Mapping[str, float] | None = None,
        model_version: str = WORKLOAD_MODEL_VERSION,
        link: str = "logistic",
    ) -> None:
        self.intercept = float(intercept)
        supplied = coefficients or DEFAULT_COEFFICIENTS
        self.coefficients = {
            name: float(supplied.get(name, 0.0)) for name in WORKLOAD_FEATURE_NAMES
        }
        self.model_version = str(model_version)
        self.link = str(link)

    def predict(self, features: Mapping[str, Any]) -> float:
        linear = self.intercept + sum(
            self.coefficients[name] * _unit(features.get(name))
            for name in WORKLOAD_FEATURE_NAMES
        )
        value = _sigmoid(linear) if self.link == "logistic" else linear
        return max(0.0, min(1.0, float(value)))

    def estimate(
        self,
        semantic_values: Mapping[str, Any] | None,
        **feature_context: Any,
    ) -> WorkloadEstimate:
        features = workload_feature_vector(semantic_values, **feature_context)
        return WorkloadEstimate(
            schema_version=WORKLOAD_SCHEMA_VERSION,
            model_version=self.model_version,
            feature_vector=features,
            workload_prior=round(self.predict(features), 6),
        )

    @classmethod
    def fit_ridge(
        cls,
        feature_rows: Sequence[Mapping[str, Any]],
        observed_workload: Sequence[float],
        *,
        alpha: float = 1.0,
    ) -> tuple["WorkloadEstimator", WorkloadFit]:
        """Fit an interpretable ridge model, leaving the intercept unpenalized."""

        if len(feature_rows) != len(observed_workload) or not feature_rows:
            raise ValueError("feature_rows and observed_workload must be non-empty and aligned")
        x = np.asarray(
            [[_unit(row.get(name)) for name in WORKLOAD_FEATURE_NAMES] for row in feature_rows],
            dtype=float,
        )
        y = np.asarray([_unit(value) for value in observed_workload], dtype=float)
        design = np.column_stack([np.ones(len(x)), x])
        penalty = np.eye(design.shape[1], dtype=float) * max(0.0, float(alpha))
        penalty[0, 0] = 0.0
        beta = np.linalg.pinv(design.T @ design + penalty) @ design.T @ y
        estimator = cls(
            intercept=float(beta[0]),
            coefficients={
                name: float(beta[index + 1])
                for index, name in enumerate(WORKLOAD_FEATURE_NAMES)
            },
            model_version="workload-ridge.v1",
            link="identity_clip",
        )
        predicted = np.asarray([estimator.predict(row) for row in feature_rows])
        residual = y - predicted
        fit = WorkloadFit(
            model_version=estimator.model_version,
            sample_count=len(feature_rows),
            ridge_alpha=max(0.0, float(alpha)),
            intercept=estimator.intercept,
            coefficients=dict(estimator.coefficients),
            mae=float(np.mean(np.abs(residual))),
            rmse=float(np.sqrt(np.mean(residual**2))),
        )
        return estimator, fit


def observed_workload(scores: Mapping[str, Any]) -> float:
    """Build Raw-TLX-style observed workload from post-event appraisal."""

    frustration = _unit(scores.get("frustration"))
    if scores.get("perceived_control") is not None:
        frustration = 0.5 * (
            frustration + (1.0 - _unit(scores.get("perceived_control")))
        )
    values = (
        _unit(scores.get("mental_demand")),
        _unit(scores.get("physical_demand")),
        _unit(scores.get("temporal_demand")),
        _unit(scores.get("effort")),
        frustration,
    )
    return round(sum(values) / len(values), 6)


def event_workload_contribution(
    workload_score: float,
    *,
    minutes_before_start: float,
    minutes_after_end: float,
    active: bool,
    pre_tau_minutes: float = 90.0,
    post_tau_minutes: float = 120.0,
) -> tuple[float, str]:
    """Return ``W_e g_e(t)`` and its active/anticipation/aftermath phase."""

    score = _unit(workload_score)
    if active:
        return score, "active"
    if minutes_before_start > 0.0:
        return score * math.exp(-minutes_before_start / max(1.0, pre_tau_minutes)), "anticipation"
    if minutes_after_end >= 0.0:
        return score * math.exp(-minutes_after_end / max(1.0, post_tau_minutes)), "aftermath"
    return 0.0, "inactive"


def apply_continuous_load(
    workload: float,
    continuous_hours: float,
    *,
    saturation_hours: float = 3.0,
    beta: float = 0.18,
) -> tuple[float, float]:
    """Apply ``clip(W + beta * min(1, h_c/h_sat), 0, 1)``."""

    continuous = min(1.0, max(0.0, float(continuous_hours)) / max(0.01, float(saturation_hours)))
    adjusted = max(0.0, min(1.0, _unit(workload) + float(beta) * continuous))
    return adjusted, continuous
