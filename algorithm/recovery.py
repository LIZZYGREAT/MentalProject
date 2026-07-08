"""Shared meal/nap recovery primitives."""

from typing import Any, Dict, Tuple

from algorithm.physiology import hill_response, recovery_curve, time_damping_curve


def trait_parameters(user: Any, strategy_name: str) -> Tuple[float, float]:
    """Return ``(eta, tau)`` for a rest strategy from ``rest_trait_modifiers``."""
    trait_cfg = user.get_param("rest_trait_modifiers", {})
    strategy = (strategy_name or "").lower()

    for key, vals in trait_cfg.items():
        if key in strategy:
            return vals.get("eta", 1.0), vals.get("tau", 1.0)

    default = trait_cfg.get("default", {"eta": 1.0, "tau": 1.0})
    return default.get("eta", 1.0), default.get("tau", 1.0)


def boosted_recovery_delta_s(
    ds_base: float,
    stress_gap: float,
    time_ratio: float,
    cfg: Dict[str, float],
    eta: float,
    tau: float,
) -> Tuple[float, float, float, float]:
    """Boost a negative rest delta using Hill response and time damping."""
    hill = hill_response(stress_gap, cfg.get("K_half", 15.0), cfg.get("hill_n", 2.0))
    curve = time_damping_curve(time_ratio, tau, cfg)
    multiplier = 1.0 + cfg.get("A_max", 1.0) * eta * hill * curve
    if ds_base < 0:
        return ds_base * multiplier, hill, curve, multiplier
    return ds_base, hill, curve, 1.0


def recovery_alpha(stress_gap: float, cfg: Dict[str, float]) -> float:
    """Energy absorption factor for meal and nap events."""
    return recovery_curve(stress_gap, cfg)

