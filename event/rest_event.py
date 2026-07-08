# event/rest_event.py
from datetime import datetime
from typing import Dict, Any, Tuple
import math
import numpy as np  
from event.base import BaseEvent
from algorithm.recovery import boosted_recovery_delta_s, recovery_alpha, trait_parameters
from algorithm.time_utils import elapsed_minutes, interval_minutes
from settings.model_defaults import DEFAULT_INITIAL_ENERGY

def _get_deterministic_rng(user, current_time: datetime) -> np.random.RandomState:
    """按用户种子与日历时刻构造可复现的 RandomState（供需要时扩展随机模块）。"""
    base_seed = int(user.get_param("random_seed", 42))
    time_hash = current_time.year * 10000 + current_time.month * 100 + current_time.day
    time_hash += current_time.hour * 60 + current_time.minute
    
    final_seed = (base_seed + time_hash) % (2**32 - 1)
    return np.random.RandomState(final_seed)

def _get_trait_parameters(user, strategy_name: str) -> Tuple[float, float]:
    """
    从 rest_trait_modifiers 匹配 strategy_name 子串，返回 (eta, tau)。
    eta：就餐/小睡里放大 Hill 减压；tau：进入时间阻尼指数。
    """
    return trait_parameters(user, strategy_name)

class RestEvent(BaseEvent):
    """泛休息：完全委托 user.rest_strategy.calculate_flow_recovery，idle_duration 累计停留时长。"""
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", 
                 metadata: Dict[str, Any] = None):
        meta = metadata or {}
        if "idle_duration" not in meta:
            meta["idle_duration"] = 0.0
        super().__init__(event_id, start_time, end_time, name, description, meta)
        
    def get_event_type(self) -> str:
        return "rest"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        time_step = user.get_param("time_step", 5)
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, DEFAULT_INITIAL_ENERGY, current_time, time_step)
        return ds
        
    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int, 
                                   step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                                   is_substep: bool = False) -> Tuple[float, float]:
        """
        [接口修改]：透传 step_noise_s 和 step_noise_e 到休息策略。
        """
        idle_dur = self.metadata.get("idle_duration", 0.0)
        S_star = user.get_param("S_star_init", 50.0)
        
        # 将步长锁定噪声下发给 RestStrategy
        ds, de = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, idle_dur, time_step, S_star, 
            step_noise_s=step_noise_s, step_noise_e=step_noise_e, is_substep=is_substep
        )
        
        if not is_substep:
            self.metadata["idle_duration"] += time_step
        return ds, de

class MealEvent(BaseEvent):
    """就餐：先取休息流对压力的基线 ds_base，再 Hill+时间曲线放大减压；dE 按餐别总恢复量均摊到时长。"""
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 meal_type: str = "normal", name: str = "就餐", description: str = "", metadata: Dict[str, Any] = None):
        meta = metadata or {}
        meta["meal_type"] = meal_type
        if "idle_duration" not in meta:
            meta["idle_duration"] = 0.0
        super().__init__(event_id, start_time, end_time, name, description, meta)
        self.meal_type = meal_type

    def get_event_type(self) -> str:
        return "meal"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        time_step = user.get_param("time_step", 5)
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, DEFAULT_INITIAL_ENERGY, current_time, time_step)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int, 
                                   step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                                   is_substep: bool = False) -> Tuple[float, float]:
        """
        [接口修改]：透传 step_noise 确保 RK4 内部计算确定性。
        """
        S_star = user.get_param("S_star_init", 50.0)
        meal_cfg = user.get_param("event_meal", {})
        idle_dur = self.metadata.get("idle_duration", 0.0)
        
        # 压力基准同样依赖步长锁定噪声
        ds_base, _ = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, idle_dur, time_step, S_star, 
            step_noise_s=step_noise_s, step_noise_e=step_noise_e, is_substep=is_substep
        )
        
        total_mins = interval_minutes(self.start_time, self.end_time, default=30.0)

        time_ratio = min(1.0, idle_dur / total_mins)
        diff = max(0.0, current_stress - S_star)

        rest_strat_name = user.get_rest_strategy()
        eta_strategy, tau_trait = trait_parameters(user, rest_strat_name)
        alpha_x = recovery_alpha(diff, meal_cfg)

        if "晚" in self.name:
            total_E_recover = user.get_param("meal_dinner_recover", 15.0)
        else:
            total_E_recover = user.get_param("meal_lunch_recover", 12.0)
            
        mult_normal = meal_cfg.get("multiplier_normal", 1.15)
        mult_late = meal_cfg.get("multiplier_late", 0.85)
        meal_multiplier = mult_normal if self.meal_type in ["normal", "early"] else mult_late
        
        delta_E = total_E_recover * meal_multiplier * alpha_x * (time_step / total_mins)

        delta_S, hill_factor, time_curve, _ = boosted_recovery_delta_s(
            ds_base, diff, time_ratio, meal_cfg, eta_strategy, tau_trait
        )

        if not is_substep:
            epoc_inj = meal_cfg.get("epoc_injection", 0.5)
            epoc_max = meal_cfg.get("epoc_max", 20.0)
            epoc_step = epoc_inj * (time_step / 5.0)
            user.epoc_level = min(epoc_max, getattr(user, 'epoc_level', 0.0) + epoc_step)
            self.metadata["idle_duration"] += time_step

            if "math_trace" not in self.metadata:
                trace_str = (
                    f"$$\\Delta S_{{base}} = {ds_base:.3f}, \\quad \\Delta S_{{final}} = {delta_S:.3f}$$<br>"
                    f"$$Hill = {hill_factor:.3f}, \\quad TimeCurve = {time_curve:.3f}$$<br>"
                    f"$$\\Delta E = Total({total_E_recover:.1f}) \\cdot M_{{time}}({meal_multiplier:.2f}) \\cdot \\alpha_{{logistic}}({alpha_x:.2f}) \\cdot \\left(\\frac{{{time_step}}}{{{total_mins:.1f}}}\\right) = {delta_E:.3f}$$"
                )
                self.metadata["math_trace"] = trace_str
            
        return delta_S, delta_E


class NapEvent(BaseEvent):
    """小睡：结构同 MealEvent，增加还债分支（减睡眠债、debt_mult 放大 dE/dS）。"""
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 nap_type: str = "proper", name: str = "午睡", description: str = "", metadata: Dict[str, Any] = None):
        meta = metadata or {}
        meta["nap_type"] = nap_type
        if "idle_duration" not in meta:
            meta["idle_duration"] = 0.0
        super().__init__(event_id, start_time, end_time, name, description, meta)
        self.nap_type = nap_type
        
    def get_event_type(self) -> str:
        return "nap"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        time_step = user.get_param("time_step", 5)
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, DEFAULT_INITIAL_ENERGY, current_time, time_step)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int, 
                                   step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                                   is_substep: bool = False) -> Tuple[float, float]:
        """
        [接口修改]：透传步长锁定随机因子。
        """
        S_star = user.get_param("S_star_init", 50.0)
        nap_cfg = user.get_param("event_nap", {})
        idle_dur = self.metadata.get("idle_duration", 0.0)
        
        ds_base, _ = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, idle_dur, time_step, S_star, 
            step_noise_s=step_noise_s, step_noise_e=step_noise_e, is_substep=is_substep
        )
        
        total_mins = interval_minutes(self.start_time, self.end_time, default=30.0)

        time_ratio = min(1.0, idle_dur / total_mins)
        diff = max(0.0, current_stress - S_star)

        rest_strat_name = user.get_rest_strategy()
        eta_strategy, tau_trait = trait_parameters(user, rest_strat_name)
        alpha_x = recovery_alpha(diff, nap_cfg)

        if self.nap_type == "proper":
            total_E_recover = user.get_param("nap_proper_recover", 20.0)
        else:
            total_E_recover = user.get_param("nap_short_recover", 12.0)

        nap_multiplier_key = "multiplier_proper" if self.nap_type == "proper" else "multiplier_short"
        nap_multiplier = nap_cfg.get(nap_multiplier_key, 1.0)
        delta_E = total_E_recover * nap_multiplier * alpha_x * (time_step / total_mins)

        is_repaying = self.metadata.get("is_repaying_debt", False)
        debt_mult = nap_cfg.get("debt_multiplier", 1.2)
        if is_repaying and user.get_sleep_debt() > 0:
            debt_k = nap_cfg.get("debt_reduce_k", 2.0)
            if not is_substep:
                user.reduce_sleep_debt((time_step / 60.0) * debt_k)
            delta_E *= debt_mult  

        delta_S, hill_factor, time_curve, _ = boosted_recovery_delta_s(
            ds_base, diff, time_ratio, nap_cfg, eta_strategy, tau_trait
        )
        
        if is_repaying and user.get_sleep_debt() > 0:
            if delta_S < 0:
                delta_S *= debt_mult

        if not is_substep:
            epoc_inj = nap_cfg.get("epoc_injection", 1.0)
            epoc_max = nap_cfg.get("epoc_max", 30.0)
            epoc_step = epoc_inj * (time_step / 5.0)
            user.epoc_level = min(epoc_max, getattr(user, 'epoc_level', 0.0) + epoc_step)
            self.metadata["idle_duration"] += time_step

            if "math_trace" not in self.metadata:
                trace_str = (
                    f"$$\\Delta S_{{base}} = {ds_base:.3f}, \\quad \\Delta S_{{final}} = {delta_S:.3f}$$<br>"
                    f"$$Hill_{{factor}} = {hill_factor:.3f}, \\quad TimeCurve = {time_curve:.3f}, \\quad Debt_{{mult}} = {debt_mult if (is_repaying and user.get_sleep_debt() > 0) else 1.0:.2f}$$<br>"
                    f"$$\\Delta E = Total({total_E_recover:.1f}) \\cdot M_{{nap}}({nap_multiplier:.2f}) \\cdot \\alpha_{{logistic}}({alpha_x:.2f}) \\cdot Debt_{{mult}} \\cdot \\left(\\frac{{{time_step}}}{{{total_mins:.1f}}}\\right) = {delta_E:.3f}$$"
                )
                self.metadata["math_trace"] = trace_str
            
        return delta_S, delta_E


class SleepEvent(BaseEvent):
    """长睡眠：用 night_strategy.calculate_step，elapsed 为自入睡时刻起算分钟数。"""
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "睡眠", description: str = "", metadata: Dict[str, Any] = None):
        super().__init__(event_id, start_time, end_time, name, description, metadata or {})

    def get_event_type(self) -> str:
        return "sleep"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        time_step = user.get_param("time_step", 5)
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, DEFAULT_INITIAL_ENERGY, current_time, time_step)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int, 
                                   step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                                   is_substep: bool = False) -> Tuple[float, float]:
        """
        [接口修改]：透传步长锁定噪声。
        """
        elapsed = elapsed_minutes(self.start_time, current_time)
        ds, de = user.night_strategy.calculate_step(
            current_stress, current_energy, current_time, time_step, elapsed, 
            step_noise_s=step_noise_s, step_noise_e=step_noise_e, is_substep=is_substep
        )
        
        if not is_substep:
            if "math_trace" not in self.metadata:
                self.metadata["math_trace"] = f"$$ NightStrategy \\Rightarrow \\Delta S: {ds:.3f}, \\Delta E: {de:.3f} $$"
                
        return ds, de
