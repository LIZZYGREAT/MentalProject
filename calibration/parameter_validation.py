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
    RangeRule("ctssm_params.stress_reactivity_per_hour", 0.05, 6.0),
    RangeRule("ctssm_params.stress_recovery_per_hour", 0.05, 4.0),
    RangeRule("ctssm_params.event_stress_gain", 0.0, 60.0),
    RangeRule("ctssm_params.m0_anticipation_stress_gain", 0.0, 20.0),
    RangeRule("ctssm_params.m0_post_event_stress_gain", 0.0, 25.0),
    RangeRule("ctssm_params.cognition_stress_gain", 0.0, 40.0),
    RangeRule("ctssm_params.fatigue_stress_gain", 0.0, 40.0),
    RangeRule("ctssm_params.sleep_debt_stress_per_hour", 0.0, 5.0),
    RangeRule("ctssm_params.vitality_baseline", 30.0, 95.0),
    RangeRule("ctssm_params.vitality_regulation_per_hour", 0.05, 4.0),
    RangeRule("ctssm_params.demand_vitality_drain_per_hour", 0.0, 40.0),
    RangeRule("ctssm_params.recovery_vitality_gain_per_hour", 0.0, 40.0),
    RangeRule("ctssm_params.fatigue_vitality_gain", 0.0, 50.0),
    RangeRule("ctssm_params.sleep_debt_vitality_per_hour", 0.0, 6.0),
    RangeRule("ctssm_params.cognition_decay_per_hour", 0.05, 6.0),
    RangeRule("ctssm_params.anticipation_gain_per_hour", 0.0, 4.0),
    RangeRule("ctssm_params.aftermath_gain_per_hour", 0.0, 4.0),
    RangeRule("ctssm_params.fatigue_accumulation_per_hour", 0.0, 3.0),
    RangeRule("ctssm_params.fatigue_recovery_per_hour", 0.0, 4.0),
    RangeRule("ctssm_params.vitality_to_stress_gain", 0.0, 1.0),
    RangeRule("ctssm_params.stress_to_vitality_gain", 0.0, 1.0),
    RangeRule("ctssm_params.cross_day_stress_persistence", 0.0, 1.0),
    RangeRule("ctssm_params.cross_day_vitality_persistence", 0.0, 1.0),
    RangeRule("ctssm_params.cross_day_cognition_persistence", 0.0, 1.0),
    RangeRule("ctssm_params.cross_day_fatigue_persistence", 0.0, 1.0),
    RangeRule("ctssm_params.cross_day_fatigue_stress_gain", 0.0, 20.0),
    RangeRule("ctssm_params.cross_day_unfinished_decay_hours", 4.0, 48.0),
    RangeRule("ctssm_params.cross_day_unfinished_input_floor", 0.0, 0.45),
    RangeRule("ctssm_params.cross_day_unfinished_sleep_multiplier", 0.0, 1.0),
    RangeRule("ctssm_params.sleep_quality_initial_stress_gain", 0.0, 20.0),
    RangeRule("ctssm_params.sleep_quality_initial_vitality_gain", 0.0, 20.0),
    RangeRule("ctssm_params.sleep_quality_event_appraisal_gain", 0.0, 0.30),
    RangeRule("ctssm_params.stress_process_sd_per_sqrt_hour", 0.0, 20.0),
    RangeRule("ctssm_params.vitality_process_sd_per_sqrt_hour", 0.0, 20.0),
    RangeRule("ctssm_params.cognition_process_sd_per_sqrt_hour", 0.0, 1.0),
    RangeRule("ctssm_params.fatigue_process_sd_per_sqrt_hour", 0.0, 1.0),
    RangeRule("ctssm_params.stress_observation_sd", 1.0, 30.0),
    RangeRule("ctssm_params.vitality_observation_sd", 1.0, 30.0),
    RangeRule("ctssm_params.cognition_observation_sd", 0.01, 1.0),
    RangeRule("ctssm_params.observation_delay_variance_per_hour", 0.0, 5.0),
    RangeRule("ctssm_params.retrospective_variance_multiplier", 0.0, 5.0),
    RangeRule("model_selection.minimum_test_days", 2.0, 365.0, integer=True),
    RangeRule("model_selection.minimum_relative_mae_improvement", 0.0, 0.5),
    RangeRule("model_selection.minimum_interval_coverage", 0.5, 0.99),
    RangeRule("alert_thresholds.recovery_stress", 30.0, 80.0),
    RangeRule("alert_thresholds.yellow_stress", 45.0, 90.0),
    RangeRule("alert_thresholds.orange_stress", 55.0, 95.0),
    RangeRule("alert_thresholds.red_stress", 65.0, 100.0),
    RangeRule("alert_thresholds.extreme_stress", 70.0, 100.0),
    RangeRule("alert_thresholds.yellow_confirm_minutes", 5.0, 120.0),
    RangeRule("alert_thresholds.orange_confirm_minutes", 5.0, 120.0),
    RangeRule("alert_thresholds.red_confirm_minutes", 0.0, 60.0),
    RangeRule("alert_thresholds.cooldown_minutes", 30.0, 720.0),
    RangeRule("alert_thresholds.max_daily_care", 0.0, 6.0, integer=True),
    RangeRule(
        "alert_thresholds.max_daily_critical_override",
        0.0,
        3.0,
        integer=True,
    ),
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

    coupling = str(
        get_nested(params, "ctssm_params.stress_vitality_coupling", "none")
    ).lower()
    if coupling not in {"none", "v_to_s", "s_to_v", "bidirectional"}:
        issues.append(
            _issue(
                "error",
                "ctssm_params.stress_vitality_coupling",
                "must be one of none, v_to_s, s_to_v, bidirectional",
                coupling,
            )
        )

    care_thresholds = [
        get_nested(params, "alert_thresholds.recovery_stress"),
        get_nested(params, "alert_thresholds.yellow_stress"),
        get_nested(params, "alert_thresholds.orange_stress"),
        get_nested(params, "alert_thresholds.red_stress"),
        get_nested(params, "alert_thresholds.extreme_stress"),
    ]
    if all(value is not None for value in care_thresholds):
        numeric_thresholds = [float(value) for value in care_thresholds]
        if numeric_thresholds != sorted(numeric_thresholds) or len(
            set(numeric_thresholds)
        ) != len(numeric_thresholds):
            issues.append(
                _issue(
                    "error",
                    "alert_thresholds",
                    (
                        "must satisfy recovery < yellow < orange < red "
                        "< extreme"
                    ),
                    numeric_thresholds,
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
