"""Micro-dynamics and buffers layered on top of integrated S/E deltas."""

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional

from algorithm.physiology import step_scale
from settings.model_defaults import RECOVERY_STATES, RESTORATIVE_STATES


@dataclass
class MicroDynamicState:
    """Mutable pools that persist across simulation steps."""

    energy_buffer: float = 0.0
    friction_excess_stress: float = 0.0
    dopamine_buffer: float = 0.0
    momentum_s_1: float = 0.0
    momentum_s_2: float = 0.0


@dataclass(frozen=True)
class MicroDynamicStepResult:
    """Final step increments after buffers, EPOC, inertia, and filters."""

    final_s_step: float
    final_e_step: float
    epoc_level: float
    momentum_trace: str = ""
    logs: List[str] = field(default_factory=list)


def trigger_epiphany_refund(
    pools: MicroDynamicState,
    cfg: Dict[str, float],
    resilience: float,
) -> float:
    """Move accumulated friction stress into the dopamine buffer on a FLOW jump."""

    refund_ratio = cfg.get("epiphany_refund_ratio", 0.8)
    refund_max = cfg.get("epiphany_refund_max", 8.0)
    base_bonus = cfg.get("epiphany_base_bonus", 2.0)
    resilience_k = cfg.get("epiphany_resilience_k", 1.5)

    refund = min(pools.friction_excess_stress * refund_ratio, refund_max)
    total_dopamine = base_bonus + refund + max(0.0, resilience) * resilience_k
    pools.dopamine_buffer += total_dopamine
    pools.friction_excess_stress = 0.0
    return total_dopamine


def apply_micro_dynamics(
    pools: MicroDynamicState,
    cfg: Dict[str, float],
    time_step: float,
    state: str,
    has_high_load: bool,
    routine_event_type: Optional[str],
    current_regime: str,
    current_stress: float,
    current_energy: float,
    stress_anchor: float,
    raw_delta_s: float,
    raw_delta_e: float,
    base_delta_s: float,
    inertia_delta_s: float,
    inertia_delta_e: float,
    epoc_level: float,
    resilience: float,
    cur_str: str,
) -> MicroDynamicStepResult:
    """Apply local micro-dynamics after RK4 has produced raw S/E deltas."""

    logs: List[str] = []
    delta_s = raw_delta_s
    delta_e = raw_delta_e

    if routine_event_type in {"meal", "nap"} and delta_e > 0:
        pools.energy_buffer += delta_e
        delta_e = 0.0

    if current_regime == "FRICTION":
        pools.friction_excess_stress += max(0.0, delta_s - base_delta_s)
    else:
        decay = cfg.get("friction_decay_rate", 0.2)
        pools.friction_excess_stress = max(
            0.0,
            pools.friction_excess_stress - decay * step_scale(time_step),
        )

    if pools.dopamine_buffer > 0:
        release_rate = cfg.get("dopamine_release_rate", 0.15)
        release = min(pools.dopamine_buffer, release_rate * step_scale(time_step))
        pools.dopamine_buffer -= release
        delta_s -= release
        delta_e += release * cfg.get("dopamine_energy_ratio", 0.4)
        if pools.dopamine_buffer <= 0.01:
            logs.append(f"[{cur_str}] 多巴胺代谢完毕。")

    delta_s, delta_e, epoc_level, epoc_logs = absorb_epoc(
        epoc_level=epoc_level,
        state=state,
        has_high_load=has_high_load,
        delta_s=delta_s,
        delta_e=delta_e,
        cfg=cfg,
        time_step=time_step,
        resilience=resilience,
        cur_str=cur_str,
    )
    logs.extend(epoc_logs)

    delta_s += inertia_delta_s
    delta_e += inertia_delta_e

    final_s_step, momentum_trace = apply_stress_momentum(
        pools=pools,
        delta_s=delta_s,
        state=state,
        has_high_load=has_high_load,
        beta=cfg.get("momentum_beta", 0.40),
    )

    final_e_step = settle_energy_step(
        pools=pools,
        delta_e=delta_e,
        current_stress=current_stress,
        current_energy=current_energy,
        stress_anchor=stress_anchor,
        state=state,
        cfg=cfg,
        time_step=time_step,
    )

    return MicroDynamicStepResult(
        final_s_step=final_s_step,
        final_e_step=final_e_step,
        epoc_level=epoc_level,
        momentum_trace=momentum_trace,
        logs=logs,
    )


def absorb_epoc(
    epoc_level: float,
    state: str,
    has_high_load: bool,
    delta_s: float,
    delta_e: float,
    cfg: Dict[str, float],
    time_step: float,
    resilience: float,
    cur_str: str,
) -> tuple:
    """Convert EPOC into recovery while the user is resting."""

    logs: List[str] = []
    is_resting = state in RECOVERY_STATES or (state == "DAY_ACTIVE" and not has_high_load)
    if is_resting and epoc_level > 0:
        consume = min(epoc_level, cfg.get("epoc_absorption_rate", 1.5) * step_scale(time_step))
        new_epoc_level = epoc_level - consume
        delta_e += consume * (cfg.get("epoc_abs_e_base", 0.6) + cfg.get("epoc_abs_e_k", 0.2) * resilience)
        delta_s -= consume * (cfg.get("epoc_abs_s_base", 0.08) + cfg.get("epoc_abs_s_k", 0.05) * resilience)
        if new_epoc_level <= 0.01 and epoc_level > 0.01:
            logs.append(f"[{cur_str}] EPOC 已吸收完毕。")
        return delta_s, delta_e, max(0.0, new_epoc_level), logs
    return delta_s, delta_e, epoc_level, logs


def apply_stress_momentum(
    pools: MicroDynamicState,
    delta_s: float,
    state: str,
    has_high_load: bool,
    beta: float,
) -> tuple:
    """Smooth stress increments outside restorative states."""

    is_physiological_rest = state in RESTORATIVE_STATES or (state == "DAY_ACTIVE" and not has_high_load)
    if is_physiological_rest:
        pools.momentum_s_1 = delta_s
        pools.momentum_s_2 = delta_s
        return delta_s, ""

    beta = max(0.0, min(0.99, float(beta)))
    pools.momentum_s_1 = beta * pools.momentum_s_1 + (1.0 - beta) * delta_s
    pools.momentum_s_2 = beta * pools.momentum_s_2 + (1.0 - beta) * pools.momentum_s_1
    final_s_step = pools.momentum_s_2
    momentum_trace = (
        f"$$ AR1(Filter): S'_{{m1}} = {beta:.2f}S_{{m1}} + {1.0-beta:.2f}\\Delta S_{{in}} = {pools.momentum_s_1:.3f} $$<br>"
        f"$$ S'_{{m2}} = {beta:.2f}S_{{m2}} + {1.0-beta:.2f}S'_{{m1}} = {final_s_step:.3f} $$"
    )
    return final_s_step, momentum_trace


def settle_energy_step(
    pools: MicroDynamicState,
    delta_e: float,
    current_stress: float,
    current_energy: float,
    stress_anchor: float,
    state: str,
    cfg: Dict[str, float],
    time_step: float,
) -> float:
    """Release buffered recovery and apply basal energy drain."""

    decay_rate = cfg.get("buffer_decay_rate", 0.05)
    buffer_release = pools.energy_buffer * (1.0 - math.exp(-decay_rate * time_step))
    pools.energy_buffer -= buffer_release

    if state in RECOVERY_STATES:
        basal_drain = 0.0
    else:
        base_basal = cfg.get("basal_drain_rate", 0.05)
        stress_gap = max(0.0, current_stress - stress_anchor)
        basal_drain = (
            base_basal
            * (1.0 + cfg.get("basal_stress_gap_k", 0.02) * stress_gap)
            * step_scale(time_step)
        )

    raw_step_e = delta_e + buffer_release - basal_drain
    if raw_step_e < 0:
        psi_e = 1.0 / (1.0 + math.exp(-(current_energy - cfg.get("lorentzian_floor_E", 15.0))))
        raw_step_e *= psi_e
    return raw_step_e
