"""Parameter validation for safer curve calibration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class RangeRule:
    path: str
    minimum: float
    maximum: float
    severity: str = "error"
    integer: bool = False


RANGE_RULES: Tuple[RangeRule, ...] = (
    RangeRule("S_star_init", 30.0, 80.0),
    RangeRule("S_threshold", 55.0, 130.0),
    RangeRule("E_critical", 5.0, 50.0),
    RangeRule("time_step", 1.0, 30.0, integer=True),
    RangeRule("noise_scale_factor", 0.0, 2.0, "warning"),
    RangeRule("K_resilience", 0.2, 3.0),
    RangeRule("D_t_course", 0.0, 3.0),
    RangeRule("D_t_task", 0.0, 3.0),
    RangeRule("course_base_drain", 0.0, 20.0),
    RangeRule("task_base_drain", 0.0, 20.0),
    RangeRule("fatigue_acceleration", 0.0, 0.5),
    RangeRule("Z_awake", 0.0, 2.0),
    RangeRule("Z_factor", 0.0, 2.0),
    RangeRule("penalty_sleep_debt.drain_k", 0.0, 0.5),
    RangeRule("penalty_sleep_debt.stress_k", 0.0, 0.5),
    RangeRule("penalty_circadian.drain_multiplier", 0.5, 3.0),
    RangeRule("penalty_circadian.stress_multiplier", 0.5, 3.0),
    RangeRule("allostatic_collapse_point", 0.05, 0.95),
    RangeRule("allostatic_collapse_steepness", 0.1, 40.0),
    RangeRule("allostatic_max_penalty", 0.0, 2.0),
    RangeRule("event_task.weight_exam", 0.1, 3.0),
    RangeRule("event_task.weight_ddl", 0.1, 3.0),
    RangeRule("event_task.weight_meeting", 0.1, 3.0),
    RangeRule("event_task.weight_homework", 0.1, 3.0),
    RangeRule("event_task.weight_general", 0.1, 3.0),
    RangeRule("event_gym.drain_rate", 0.0, 20.0),
    RangeRule("event_gym.epoc_rate", 0.0, 1.0),
    RangeRule("event_library.base_drain_rate", 0.0, 10.0),
    RangeRule("event_library.base_stress_rate", 0.0, 10.0),
    RangeRule("rest_ode_params.R_max_base", 0.0, 20.0),
    RangeRule("habituation_params.floor_mu_course", 0.0, 1.0),
    RangeRule("habituation_params.floor_mu_task", 0.0, 1.0),
    RangeRule("habituation_params.t_half_hyperbolic", 1.0, 240.0),
    RangeRule("simulator_micro_params.buffer_decay_rate", 0.0, 1.0),
    RangeRule("simulator_micro_params.basal_drain_rate", 0.0, 3.0),
    RangeRule("simulator_micro_params.basal_stress_gap_k", 0.0, 0.2),
    RangeRule("simulator_micro_params.epoc_absorption_rate", 0.0, 10.0),
    RangeRule("simulator_micro_params.momentum_beta", 0.0, 0.99),
    RangeRule("markov_semi_params.poisson_anomaly_prob", 0.0, 0.2),
)


def validate_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return validation issues for a params dictionary."""

    issues: List[Dict[str, Any]] = []
    for rule in RANGE_RULES:
        value = get_nested(params, rule.path)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            issues.append(
                _issue(rule.severity, rule.path, "must be numeric", value)
            )
            continue
        if numeric < rule.minimum or numeric > rule.maximum:
            issues.append(
                _issue(
                    rule.severity,
                    rule.path,
                    f"must be within [{rule.minimum}, {rule.maximum}]",
                    value,
                )
            )
        if rule.integer and int(numeric) != numeric:
            issues.append(_issue(rule.severity, rule.path, "must be an integer", value))

    s_star = get_nested(params, "S_star_init")
    threshold = get_nested(params, "S_threshold")
    if s_star is not None and threshold is not None:
        if float(threshold) <= float(s_star) + 10.0:
            issues.append(
                _issue(
                    "error",
                    "S_threshold",
                    "should be at least 10 points above S_star_init",
                    threshold,
                )
            )

    time_step = get_nested(params, "time_step")
    if time_step is not None:
        ts = int(float(time_step))
        if ts > 0 and 60 % ts != 0:
            issues.append(
                _issue(
                    "warning",
                    "time_step",
                    "does not divide 60 cleanly; traces and hourly scaling may be harder to interpret",
                    time_step,
                )
            )

    errors = [issue for issue in issues if issue["severity"] == "error"]
    return {
        "valid": not errors,
        "issue_count": len(issues),
        "error_count": len(errors),
        "issues": issues,
    }


def clamp_to_rules(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy with known numeric paths clamped into safe ranges."""

    cleaned = deepcopy(params)
    for rule in RANGE_RULES:
        value = get_nested(cleaned, rule.path)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        numeric = max(rule.minimum, min(rule.maximum, numeric))
        if rule.integer:
            numeric = int(round(numeric))
        set_nested(cleaned, rule.path, numeric)
    return cleaned


def get_nested(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def set_nested(data: Dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = path.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def iter_rule_paths() -> Iterable[str]:
    return (rule.path for rule in RANGE_RULES)


def _issue(severity: str, path: str, message: str, value: Any) -> Dict[str, Any]:
    return {
        "severity": severity,
        "path": path,
        "message": message,
        "value": value,
    }
