# event/library_event.py
import math
import numpy as np
from datetime import datetime
from typing import Dict, Any, Tuple
from event.base import BaseEvent
from algorithm.physiology import bounded_stress_step, hourly_scale, step_scale
from algorithm.time_utils import interval_minutes
from settings.model_defaults import DEFAULT_INITIAL_ENERGY

class LibraryEvent(BaseEvent):
    """
    图书馆自习事件 (Library) - 特质焦虑与心流交互版
    """
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "自习", description: str = "", 
                 study_intensity: float = 0.7, metadata: Dict[str, Any] = None):
        super().__init__(event_id, start_time, end_time, name, description, metadata or {})
        self.provided_intensity = max(0.1, min(1.0, float(study_intensity)))
        
        self.total_duration_mins = interval_minutes(self.start_time, self.end_time, default=120.0)
            
        self.metadata["detail"] = "推演中..." 
    
    def get_event_type(self) -> str:
        return "library"
        
    def get_fatigue_weight(self) -> float:
        intensity = self.provided_intensity if "mock" in self.event_id.lower() else 0.7
        if not hasattr(self, '_cached_user') or self._cached_user is None:
            return 0.4 + 0.4 * intensity
        lib_cfg = self._cached_user.get_param("event_library", {})
        base = lib_cfg.get("trait_weight_base", 0.4)
        k = lib_cfg.get("trait_weight_k", 0.4)
        return base + k * intensity

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        time_step = user.get_param("time_step", 5)
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, DEFAULT_INITIAL_ENERGY, current_time, time_step)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int, 
                                   step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                                   is_substep: bool = False) -> Tuple[float, float]:
                                   
        self._cached_user = user
        lib_cfg = user.get_param("event_library", {})
        
        if "mock" in self.event_id.lower():
            intensity = self.provided_intensity
            intensity_type = "沙盒注入"
        else:
            hours = self.total_duration_mins / 60.0
            focus_base = lib_cfg.get("focus_base", 0.95)
            focus_decay = lib_cfg.get("focus_decay_rate", 0.15)
            focus_min = lib_cfg.get("focus_min", 0.45)
            intensity = max(focus_min, focus_base - (hours * focus_decay))
            intensity_type = "时长衰减"
            
        resilience = user.get_resilience_index()
        trait_str = "坚韧" if resilience > 0.2 else ("焦虑" if resilience < -0.2 else "中性")
        
        if not is_substep:
            self.metadata["detail"] = f"专注度:{intensity:.2f}({intensity_type}) | 特质:{trait_str}({resilience:.2f})"
            self.metadata["weight_factor"] = f"打折注水({self.get_fatigue_weight():.2f})"

        S_star = user.get_param("S_star_init", 50.0)
        diff = current_stress - S_star
        
        base_stress_rate = lib_cfg.get("base_stress_rate", 0.60)
        base_stress_increase = base_stress_rate * intensity 
        
        flow_relief_k = lib_cfg.get("flow_relief_k", 0.020)
        flow_relief = 0.0
        if diff > 0:
            flow_relief = flow_relief_k * resilience * diff * intensity
            
        amp = 1.0
        if hasattr(user.course_strategy, 'get_allostatic_stress_amplifier'):
            amp = user.course_strategy.get_allostatic_stress_amplifier(current_energy)
            
        raw_delta_S = (base_stress_increase - flow_relief) * amp * step_scale(time_step)
        
        sleep_debt = user.get_sleep_debt()
        if sleep_debt > 0 and raw_delta_S > 0:
            penalty_sleep = user.get_param("penalty_sleep_debt", {})
            stress_k = penalty_sleep.get("stress_k", 0.04)
            raw_delta_S *= (1.0 + stress_k * sleep_debt)
        
        max_s_step = lib_cfg.get("max_s_step", 1)
        delta_S = bounded_stress_step(raw_delta_S, max_s_step)
        
        noise_s = step_noise_s * 0.05
        delta_S += noise_s
        
        K_resilience = user.get_param("K_resilience", 1.0)
        lib_drain = lib_cfg.get("base_drain_rate", 0.75)  
        drain_rate = (lib_drain * intensity) / K_resilience
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
            
        if sleep_debt > 0:
            penalty_sleep = user.get_param("penalty_sleep_debt", {})
            drain_k = penalty_sleep.get("drain_k", 0.05)
            drain_rate *= (1.0 + drain_k * sleep_debt)
            
        noise_e = step_noise_e * 0.05
        delta_E = -drain_rate * f_drain_modifier * hourly_scale(time_step) + noise_e


        if not is_substep:
            if "math_trace" not in self.metadata:
                if flow_relief >= 0:
                    flow_str = f" - Flow({flow_relief:.3f})"
                else:
                    flow_str = f" + Friction({abs(flow_relief):.3f})"

                # 最终组装公式
                trace_str = (
                    f"$$S_{{raw}} = (Base({base_stress_increase:.3f}){flow_str}) \\cdot Amp({amp:.2f}) = {raw_delta_S:.3f}$$"
                    f"$$\\Delta S = Limit({max_s_step:.1f}) \\cdot \\tanh\\left(\\frac{{{raw_delta_S:.3f}}}{{{max_s_step:.1f}}}\\right) + Noise({noise_s:.2f}) = {delta_S:.3f}$$<br>"
                    f"$$\\Delta E = -Drain({drain_rate:.3f}) \\cdot f_{{drain}}({f_drain_modifier:.2f}) \\cdot \\left(\\frac{{{time_step}}}{{60.0}}\\right) + Noise({noise_e:.2f}) = {delta_E:.3f}$$"
                )
                self.metadata["math_trace"] = trace_str
        
        return delta_S, delta_E
