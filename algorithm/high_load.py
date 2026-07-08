"""Shared course/task high-load dynamics."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Tuple

from algorithm.physiology import (
    circadian_multiplier,
    environment_z_multiplier,
    hourly_scale,
    hyperbolic_habituation,
    se_pair,
    sleep_debt_multiplier,
    step_scale,
)
from algorithm.time_utils import elapsed_minutes


@dataclass(frozen=True)
class HighLoadProfile:
    """Parameters needed to evaluate a high-load event step."""

    load_weight: float
    d_t_key: str
    d_t_default: float
    base_drain_key: str
    base_drain_default: float
    habit_floor_key: str
    trace_label: str


def _energy_drain_modifier(user: Any, current_energy: float) -> float:
    if hasattr(user.course_strategy, "get_energy_drain_modifier"):
        return user.course_strategy.get_energy_drain_modifier(current_energy)
    return 1.0


def fatigue_acceleration_multiplier(user: Any, elapsed_min: float) -> float:
    """Return within-event drain acceleration, normalized as a per-hour slope.

    Historical config used ``fatigue_acceleration=1.15`` as a multiplier-like
    value. The optimized model interprets values above 1 as ``value - 1`` slope,
    while values in [0, 1] are treated directly as slope.
    """
    raw = float(user.get_param("fatigue_acceleration", 0.15))
    slope = raw - 1.0 if raw > 1.0 else raw
    slope = max(0.0, min(0.5, slope))
    return 1.0 + slope * (max(0.0, elapsed_min) / 60.0)


def calculate_high_load_impact(
    user: Any,
    current_stress: float,
    current_energy: float,
    current_time: datetime,
    time_step: int,
    event_start_time: Any,
    profile: HighLoadProfile,
    step_noise_s: float = 0.0,
) -> Tuple[float, float, Dict[str, float]]:
    """Calculate common dS/dE mechanics for course and task events."""
    s_star = user.get_param("S_star_init", 50.0)
    k_resilience = user.get_param("K_resilience", 1.0)
    elapsed_min = elapsed_minutes(event_start_time, current_time)

    debt_cfg = user.get_param("penalty_sleep_debt", {})
    debt_mult = sleep_debt_multiplier(
        user.get_sleep_debt(),
        slope=debt_cfg.get("drain_k", 0.10),
    )
    stress_debt_mult = sleep_debt_multiplier(
        user.get_sleep_debt(),
        slope=debt_cfg.get("stress_k", 0.04),
    )
    circadian_mult = circadian_multiplier(
        current_time.hour,
        user.get_param("penalty_circadian", {}),
        kind="drain",
    )
    acc_mult = fatigue_acceleration_multiplier(user, elapsed_min)
    base_drain = user.get_param(profile.base_drain_key, profile.base_drain_default)
    drain_modifier = _energy_drain_modifier(user, current_energy)
    linear_drain_rate = (base_drain * profile.load_weight * acc_mult) / k_resilience

    delta_e = -linear_drain_rate * drain_modifier * debt_mult * circadian_mult * hourly_scale(time_step)

    d_t = user.get_param(profile.d_t_key, profile.d_t_default)
    f_s_val = user.course_strategy.f_s(current_stress, current_energy, s_star, step_noise_s=step_noise_s)
    z_mult = environment_z_multiplier(
        user.get_param("Z_awake", 0.5),
        user.get_param("Z_factor", 0.5),
    )
    habit_cfg = user.get_param("habituation_params", {})
    theta = hyperbolic_habituation(
        elapsed_min,
        habit_cfg.get(profile.habit_floor_key, 0.35),
        habit_cfg.get("t_half_hyperbolic", 40.0),
    )

    s_dot = f_s_val * profile.load_weight * d_t * z_mult * theta * stress_debt_mult
    delta_s = s_dot * step_scale(time_step)

    trace = {
        "S_dot": s_dot,
        "D_t": d_t,
        "load_weight": profile.load_weight,
        "f_s": f_s_val,
        "Z_env": z_mult,
        "Theta": theta,
        "base_drain": base_drain,
        "acc_multiplier": acc_mult,
        "K_resilience": k_resilience,
        "f_drain_modifier": drain_modifier,
        "f_debt_drain": debt_mult,
        "f_debt_stress": stress_debt_mult,
        "f_circadian_drain": circadian_mult,
        "elapsed_min": elapsed_min,
        "linear_drain_rate": linear_drain_rate,
    }
    return se_pair(delta_s, delta_e) + (trace,)
