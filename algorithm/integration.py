"""Numerical integration helpers used by the simulation engine."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Tuple


DerivativeEvaluator = Callable[
    [float, float, datetime, float, bool],
    Tuple[float, float, float, float, list],
]


@dataclass(frozen=True)
class Rk4StepResult:
    """Integrated S/E increment plus first-stage trace metadata."""

    delta_s: float
    delta_e: float
    base_delta_s: float
    fatigue_penalty: float
    components: List[tuple]


def rk4_step(
    evaluate: DerivativeEvaluator,
    stress: float,
    energy: float,
    current_time: datetime,
    time_step_minutes: float,
    elapsed_minutes: float,
) -> Rk4StepResult:
    """Integrate one S/E step with classical RK4.

    ``evaluate`` receives the temporary stress/energy, sample time, elapsed
    minutes inside the current physiological state, and whether it is a
    sub-step. The first-stage metadata is returned because event profiling and
    trace output should describe the real step boundary, not an RK4 midpoint.
    """

    k1_s, k1_e, base_delta_s, fatigue_penalty, components = evaluate(
        stress,
        energy,
        current_time,
        elapsed_minutes,
        False,
    )

    half_step = time_step_minutes / 2.0
    mid_time = current_time + timedelta(minutes=half_step)
    mid_elapsed = elapsed_minutes + half_step

    k2_s, k2_e, _, _, _ = evaluate(
        stress + k1_s / 2.0,
        energy + k1_e / 2.0,
        mid_time,
        mid_elapsed,
        True,
    )
    k3_s, k3_e, _, _, _ = evaluate(
        stress + k2_s / 2.0,
        energy + k2_e / 2.0,
        mid_time,
        mid_elapsed,
        True,
    )

    end_time = current_time + timedelta(minutes=time_step_minutes)
    end_elapsed = elapsed_minutes + time_step_minutes
    k4_s, k4_e, _, _, _ = evaluate(
        stress + k3_s,
        energy + k3_e,
        end_time,
        end_elapsed,
        True,
    )

    return Rk4StepResult(
        delta_s=(k1_s + 2.0 * k2_s + 2.0 * k3_s + k4_s) / 6.0,
        delta_e=(k1_e + 2.0 * k2_e + 2.0 * k3_e + k4_e) / 6.0,
        base_delta_s=base_delta_s,
        fatigue_penalty=fatigue_penalty,
        components=components,
    )
