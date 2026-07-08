"""Lightweight parameter calibration using feedback-scored simulations."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import random
from typing import Any, Dict, Iterable, List, Optional, Tuple

from calibration.metrics import aggregate_metrics, evaluate_simulation
from calibration.parameter_validation import (
    clamp_to_rules,
    get_nested,
    set_nested,
    validate_params,
)
from calibration.simulation_runner import run_simulation_for_calibration


DEFAULT_SEARCH_SPACE: Dict[str, Tuple[float, float]] = {
    "S_star_init": (40.0, 70.0),
    "S_threshold": (70.0, 115.0),
    "D_t_course": (0.25, 1.60),
    "D_t_task": (0.20, 1.40),
    "course_base_drain": (2.0, 12.0),
    "task_base_drain": (2.0, 12.0),
    "fatigue_acceleration": (0.0, 0.35),
    "K_resilience": (0.5, 2.0),
    "penalty_sleep_debt.drain_k": (0.0, 0.20),
    "penalty_sleep_debt.stress_k": (0.0, 0.20),
    "habituation_params.floor_mu_course": (0.15, 0.75),
    "habituation_params.floor_mu_task": (0.15, 0.75),
    "habituation_params.t_half_hyperbolic": (15.0, 120.0),
    "rest_ode_params.R_max_base": (2.0, 10.0),
    "simulator_micro_params.basal_drain_rate": (0.05, 0.80),
    "simulator_micro_params.buffer_decay_rate": (0.01, 0.20),
    "simulator_micro_params.basal_stress_gap_k": (0.0, 0.08),
    "simulator_micro_params.momentum_beta": (0.02, 0.45),
}


def calibrate_parameters(
    samples: Iterable[Dict[str, Any]],
    base_params: Dict[str, Any],
    search_space: Optional[Dict[str, Tuple[float, float]]] = None,
    iterations: int = 60,
    seed: int = 42,
) -> Dict[str, Any]:
    """Search for lower-loss parameters against feedback samples.

    This intentionally uses deterministic stochastic search instead of a heavy
    optimizer so the project remains easy to run on a student laptop.
    """

    sample_list = list(samples)
    if not sample_list:
        raise ValueError("calibration requires at least one sample")

    rng = random.Random(seed)
    space = search_space or DEFAULT_SEARCH_SPACE
    started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    base_candidate = clamp_to_rules(deepcopy(base_params))
    base_metrics = evaluate_candidate(base_candidate, sample_list)
    best_params = base_candidate
    best_metrics = base_metrics
    best_loss = float(base_metrics.get("total_loss", 100.0))

    trials: List[Dict[str, Any]] = [
        {"iteration": 0, "loss": best_loss, "metrics": best_metrics, "source": "base"}
    ]

    total_iterations = max(1, int(iterations))
    for iteration in range(1, total_iterations + 1):
        shrink = _phase_shrink(iteration, total_iterations)
        use_global = iteration <= max(3, total_iterations // 5) or iteration % 7 == 0
        candidate = sample_candidate(
            base_params=base_candidate,
            center_params=best_params,
            search_space=space,
            rng=rng,
            shrink=shrink,
            use_global=use_global,
        )
        validation = validate_params(candidate)
        if not validation["valid"]:
            trials.append(
                {
                    "iteration": iteration,
                    "loss": None,
                    "metrics": None,
                    "source": "invalid",
                    "validation": validation,
                }
            )
            continue

        metrics = evaluate_candidate(candidate, sample_list)
        loss = float(metrics.get("total_loss", 100.0))
        trials.append(
            {
                "iteration": iteration,
                "loss": loss,
                "metrics": metrics,
                "source": "global" if use_global else "local",
            }
        )
        if loss < best_loss:
            best_loss = loss
            best_params = candidate
            best_metrics = metrics

    ended_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return {
        "started_at": started_at,
        "ended_at": ended_at,
        "iterations": total_iterations,
        "sample_count": len(sample_list),
        "search_space": space,
        "base_loss": base_metrics.get("total_loss"),
        "best_loss": best_loss,
        "base_metrics": base_metrics,
        "best_metrics": best_metrics,
        "best_params": best_params,
        "trials": trials,
        "validation": validate_params(best_params),
    }


def evaluate_candidate(params: Dict[str, Any], samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    metrics = []
    for sample in samples:
        sample_params = _deep_merge(deepcopy(params), sample.get("user_profile", {}))
        simulation = run_simulation_for_calibration(
            date_str=sample["date"],
            events_json=sample.get("events", []),
            user_params=sample_params,
            yesterday_state=sample.get("yesterday_state"),
            weave_routines=sample.get("weave_routines", True),
        )
        metrics.append(
            evaluate_simulation(
                simulation["results"],
                simulation["alerts"],
                sample.get("feedback", {}),
            )
        )
    return aggregate_metrics(metrics)


def sample_candidate(
    base_params: Dict[str, Any],
    center_params: Dict[str, Any],
    search_space: Dict[str, Tuple[float, float]],
    rng: random.Random,
    shrink: float,
    use_global: bool,
) -> Dict[str, Any]:
    candidate = deepcopy(base_params)
    for path, bounds in search_space.items():
        low, high = float(bounds[0]), float(bounds[1])
        if use_global:
            value = rng.uniform(low, high)
        else:
            center = get_nested(center_params, path, get_nested(base_params, path, (low + high) / 2.0))
            try:
                center = float(center)
            except (TypeError, ValueError):
                center = (low + high) / 2.0
            radius = (high - low) * 0.5 * max(0.05, shrink)
            value = rng.uniform(max(low, center - radius), min(high, center + radius))
        set_nested(candidate, path, round(value, 6))
    return clamp_to_rules(candidate)


def _phase_shrink(iteration: int, total: int) -> float:
    progress = iteration / max(1, total)
    if progress < 0.35:
        return 1.0
    if progress < 0.70:
        return 0.45
    return 0.18


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base
