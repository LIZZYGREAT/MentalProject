# event/library_event.py
import math
import numpy as np
from datetime import datetime
from typing import Dict, Any, Tuple
from event.base import BaseEvent

class LibraryEvent(BaseEvent):
    """
    图书馆自习事件 (Library) - 特质焦虑与心流交互版 (带时空底噪)
    特点：
    1. 动态专注度：沙盒注入使用给定值，真实日程基于时长衰减。
    2. 性格绑定：直接拉取全局 User 缓存的特质韧性指数。
    3. 心流动力学：高压状态下，坚韧者通过行动获得掌控感，焦虑者陷入书本恐慌。
    4. 非稳态崩溃：接入全局压力放大器，精力空仓时学习压力指数级爆发。
    """
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "自习", description: str = "", 
                 study_intensity: float = 0.7, metadata: Dict[str, Any] = None):
        super().__init__(event_id, start_time, end_time, name, description, metadata or {})
        self.provided_intensity = max(0.1, min(1.0, float(study_intensity)))
        
        try:
            if isinstance(self.start_time, str):
                st_dt = datetime.strptime(self.start_time[-5:], "%H:%M")
                et_dt = datetime.strptime(self.end_time[-5:], "%H:%M")
                dur = (et_dt - st_dt).total_seconds() / 60.0
                if dur < 0: dur += 24 * 60
                self.total_duration_mins = dur
            else:
                self.total_duration_mins = (self.end_time - self.start_time).total_seconds() / 60.0
        except Exception:
            self.total_duration_mins = 120.0
            
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
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        self._cached_user = user
        lib_cfg = user.get_param("event_library", {})
        
        seed_val = int(user.get_param("random_seed", 42))
        time_hash = current_time.hour * 60 + current_time.minute + sum(ord(c) for c in self.event_id)
        rng = np.random.RandomState((seed_val + time_hash) % (2**32 - 1))
        
        if "mock" in self.event_id.lower():
            intensity = self.provided_intensity
            intensity_type = "沙盒注入"
        else:
            hours = self.total_duration_mins / 60.0
            focus_base = lib_cfg.get("focus_base", 0.95)
            focus_decay = lib_cfg.get("focus_decay_rate", 0.12)
            focus_min = lib_cfg.get("focus_min", 0.40)
            intensity = max(focus_min, focus_base - (hours * focus_decay))
            intensity_type = "时长衰减"
            
        resilience = user.get_resilience_index()
        
        trait_str = "坚韧" if resilience > 0.2 else ("焦虑" if resilience < -0.2 else "中性")
        self.metadata["detail"] = f"专注度:{intensity:.2f}({intensity_type}) | 特质:{trait_str}({resilience:.2f})"
        self.metadata["weight_factor"] = f"打折注水({self.get_fatigue_weight():.2f})"

        S_star = user.get_param("S_star_init", 50.0)
        diff = current_stress - S_star
        
        base_stress_rate = lib_cfg.get("base_stress_rate", 0.15)
        base_stress_increase = base_stress_rate * intensity 
        
        flow_relief_k = lib_cfg.get("flow_relief_k", 0.008)
        flow_relief = 0.0
        if diff > 0:
            flow_relief = flow_relief_k * resilience * diff * intensity
            
        amp = 1.0
        if hasattr(user.course_strategy, 'get_allostatic_stress_amplifier'):
            amp = user.course_strategy.get_allostatic_stress_amplifier(current_energy)
            
        raw_delta_S = (base_stress_increase - flow_relief) * amp * (time_step / 5.0)
        
        sleep_debt = user.get_sleep_debt()
        if sleep_debt > 0 and raw_delta_S > 0:
            penalty_sleep = user.get_param("penalty_sleep_debt", {})
            stress_k = penalty_sleep.get("stress_k", 0.04)
            raw_delta_S *= (1.0 + stress_k * sleep_debt)
        
        max_s_step = lib_cfg.get("max_s_step", 1.5)
        delta_S = max_s_step * math.tanh(raw_delta_S / max_s_step)
        
        noise_s = rng.normal(0, 0.08)
        delta_S += noise_s
        
        K_resilience = user.get_param("K_resilience", 1.0)
        lib_drain = lib_cfg.get("base_drain_rate", 0.72)  
        drain_rate = (lib_drain * intensity) / K_resilience
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
            
        if sleep_debt > 0:
            penalty_sleep = user.get_param("penalty_sleep_debt", {})
            drain_k = penalty_sleep.get("drain_k", 0.05)
            drain_rate *= (1.0 + drain_k * sleep_debt)
            
        noise_e = rng.normal(0, 0.05)
        delta_E = -drain_rate * f_drain_modifier * (time_step / 60.0) + noise_e
        
        return delta_S, delta_E