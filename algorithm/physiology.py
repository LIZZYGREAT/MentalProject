"""Reusable physiology and numerical helpers for S/E dynamics."""

import math
from typing import Dict, Tuple

from settings.model_defaults import (
    ENERGY_MAX,
    ENERGY_MIN,
    STRESS_FLOOR_MARGIN,
    STRESS_MAX,
    STRESS_MIN,
)


def clamp(value: float, lower: float, upper: float) -> float:
    """Clamp ``value`` into the closed interval ``[lower, upper]``."""
    return max(lower, min(upper, value))


def clamp_stress(value: float) -> float:
    """Clamp simulated stress to the model's valid range."""
    return clamp(value, STRESS_MIN, STRESS_MAX)


def clamp_energy(value: float) -> float:
    """Clamp simulated energy to the model's valid range."""
    return clamp(value, ENERGY_MIN, ENERGY_MAX)


def step_scale(time_step: float, base_minutes: float = 5.0) -> float:
    """Scale a per-5-minute model increment to the current step size."""
    return float(time_step) / base_minutes


def hourly_scale(time_step: float) -> float:
    """Scale an hourly rate to the current step size."""
    return float(time_step) / 60.0


def sleep_debt_multiplier(debt_hours: float, slope: float = 0.10) -> float:
    """Multiplier for debt-amplified drain or stress."""
    return 1.0 + slope * max(0.0, float(debt_hours))


def circadian_multiplier(current_hour: int, cfg: Dict[str, float], kind: str = "drain") -> float:
    """Return late-night circadian multiplier from global penalty config."""
    if current_hour >= 22 or current_hour < 6:
        key = f"{kind}_multiplier"
        return float(cfg.get(key, 1.0))
    return 1.0


def environment_z_multiplier(z_awake: float, z_factor: float) -> float:
    """Map environmental load to a bounded multiplier used by event stress rates."""
    z_raw = max(0.0, z_awake * z_factor)
    return 0.8 + 0.4 * (math.log1p(z_raw) / math.log(2.0))


def hyperbolic_habituation(elapsed_min: float, floor_mu: float, t_half: float) -> float:
    """Hyperbolic stimulus habituation that decays toward ``floor_mu``."""
    t_half = max(1.0, float(t_half))
    elapsed_min = max(0.0, float(elapsed_min))
    floor_mu = clamp(float(floor_mu), 0.0, 1.0)
    return floor_mu + (1.0 - floor_mu) * (t_half / (t_half + elapsed_min))


def bounded_logistic(x: float, k: float, midpoint: float, lower: float = 0.0, upper: float = 1.0) -> float:
    """Numerically stable logistic curve mapped into ``[lower, upper]``."""
    exponent = clamp(k * (x - midpoint), -50.0, 50.0)
    return lower + (upper - lower) / (1.0 + math.exp(exponent))


def hill_response(x: float, k_half: float, n: float) -> float:
    """Hill response in [0, 1], used for stronger recovery at higher stress gaps."""
    x = max(0.0, float(x))
    if x <= 0:
        return 0.0
    k_half = max(1e-6, float(k_half))
    n = max(1e-6, float(n))
    numerator = x ** n
    return numerator / (k_half ** n + numerator)


def recovery_curve(gap: float, cfg: Dict[str, float]) -> float:
    """Logistic recovery dampener: high stress suppresses direct energy gain."""
    logistic_min = cfg.get("logistic_min", 0.75)
    logistic_mid = cfg.get("logistic_mid", 25.0)
    logistic_k = cfg.get("logistic_k", 0.15)
    return bounded_logistic(gap, logistic_k, logistic_mid, lower=logistic_min, upper=1.0)


def time_damping_curve(time_ratio: float, tau: float, cfg: Dict[str, float]) -> float:
    """Exponential time damping for meal and nap recovery effects."""
    b = cfg.get("time_damp_b", 0.3)
    lam = cfg.get("time_damp_lambda", 2.0)
    ratio = clamp(time_ratio, 0.0, 1.0)
    tau = max(0.1, float(tau))
    return b + (1.0 - b) * math.exp(-lam * (ratio ** tau))


def clamp_delta_to_stress_floor(current_stress: float, delta_s: float, s_star: float, margin: float = STRESS_FLOOR_MARGIN) -> float:
    """Prevent recovery-style deltas from pulling stress unrealistically far below S*."""
    floor = s_star - margin
    if current_stress + delta_s < floor:
        return floor - current_stress
    return delta_s


def bounded_stress_step(raw_delta_s: float, max_step: float) -> float:
    """Compress extreme stress increments with a smooth tanh limiter."""
    max_step = max(1e-6, float(max_step))
    return max_step * math.tanh(raw_delta_s / max_step)


def se_pair(delta_s: float, delta_e: float) -> Tuple[float, float]:
    """Small readability helper for functions returning S/E increments."""
    return float(delta_s), float(delta_e)

