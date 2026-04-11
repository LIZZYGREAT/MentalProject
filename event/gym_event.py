# event/gym_event.py
import math
import numpy as np
from datetime import datetime
from typing import Dict, Any, Tuple
from event.base import BaseEvent

class GymEvent(BaseEvent):
    """
    体育运动事件 (Gym) - 物理消耗与EPOC注射版
    """
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "运动", description: str = "", 
                 intensity: float = 0.7, metadata: Dict[str, Any] = None):
        super().__init__(event_id, start_time, end_time, name, description, metadata or {})
        self.intensity = max(0.1, min(1.0, float(intensity)))
        
        self.metadata["weight_factor"] = f"强效冷却({self.get_fatigue_weight():.1f})"
        self.metadata["detail"] = f"强度: {self.intensity:.1f}"
    
    def get_event_type(self) -> str:
        return "gym"
        
    def get_fatigue_weight(self) -> float:
        """负权重：削减连续负荷累计。"""
        if not hasattr(self, '_cached_user') or self._cached_user is None:
            return -2.0 * (1.0 + self.intensity)
        gym_cfg = self._cached_user.get_param("event_gym", {})
        k = gym_cfg.get("fatigue_weight_k", -2.0)
        return k * (1.0 + self.intensity)
    
    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        time_step = user.get_param("time_step", 5)
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, time_step)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int, 
                                   step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                                   is_substep: bool = False) -> Tuple[float, float]:
        """
        [重构]：移除内部 rng.normal，改用传入的 step_noise 缩放。
        """
        self._cached_user = user
        S_star = user.get_param("S_star_init", 50.0)
        K_resilience = user.get_param("K_resilience", 1.0)
        gym_cfg = user.get_param("event_gym", {})
        
        ns_base = gym_cfg.get("noise_s_base", 0.15)
        ns_k = gym_cfg.get("noise_s_k", 0.10)
        ne_base = gym_cfg.get("noise_e_base", 0.10)
        ne_k = gym_cfg.get("noise_e_k", 0.10)
        
        noise_s = step_noise_s * (ns_base + ns_k * self.intensity)
        noise_e = step_noise_e * (ne_base + ne_k * self.intensity)

        stress_gap = max(0.0, current_stress - S_star)
        
        relief_factor = gym_cfg.get("relief_factor", 0.02) * self.intensity 
        delta_S = -relief_factor * stress_gap * (time_step / 5.0) + noise_s
        
        base_gym_drain = gym_cfg.get("drain_rate", 5.5)
        drain_rate = base_gym_drain * self.intensity / K_resilience
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
            
        delta_E = -drain_rate * f_drain_modifier * (time_step / 60.0) + noise_e

        if current_stress + delta_S < S_star - 5.0:
            delta_S = max(delta_S, (S_star - 5.0) - current_stress)
            
        if not is_substep:
            epoc_base = gym_cfg.get("epoc_base", 1.5)
            epoc_k = gym_cfg.get("epoc_k", 2.0)
            epoc_rate = user.get_param("gym_epoc_rate", 0.05)
            
            buff_add = (epoc_base + epoc_k * self.intensity + epoc_rate) * (time_step / 5.0)
            user.epoc_level = min(100.0, getattr(user, 'epoc_level', 0.0) + buff_add)

            if "math_trace" not in self.metadata:
                trace_str = (
                    f"$$\\Delta S = -Relief({relief_factor:.3f}) \\cdot Gap({stress_gap:.1f}) \\cdot \\left(\\frac{{{time_step}}}{{5.0}}\\right) + Noise({noise_s:.2f}) = {delta_S:.3f}$$<br>"
                    f"$$\\Delta E = -Drain({drain_rate:.3f}) \\cdot f_{{drain}}({f_drain_modifier:.2f}) \\cdot \\left(\\frac{{{time_step}}}{{60.0}}\\right) + Noise({noise_e:.2f}) = {delta_E:.3f}$$<br>"
                    f"$$EPOC_{{add}} = (Base({epoc_base:.1f}) + k({epoc_k:.1f}) \\cdot I({self.intensity:.1f}) + Rate({epoc_rate:.2f})) \\cdot \\left(\\frac{{{time_step}}}{{5.0}}\\right) = {buff_add:.3f}$$"
                )
                self.metadata["math_trace"] = trace_str
            
        return delta_S, delta_E