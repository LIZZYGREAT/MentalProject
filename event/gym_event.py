# event/gym_event.py
import math
import numpy as np
from datetime import datetime
from typing import Dict, Any, Tuple
from event.base import BaseEvent

class GymEvent(BaseEvent):
    """
    体育运动事件 (Gym) - 物理消耗与EPOC注射版
    特点：
    1. 连轴冷却器：强力倒扣连续负荷蓄水池。
    2. 即时效果：微弱降压甚至由于生理负荷持平，伴随稳定的精力消耗。
    3. EPOC注射：每分钟运动向用户的生态池注入后燃Buff，为后续的休息增幅。
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
        return -2.0 * (1.0 + self.intensity)
    
    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        S_star = user.get_param("S_star_init", 50.0)
        K_resilience = user.get_param("K_resilience", 1.0)
        
        seed_val = int(user.get_param("random_seed", 42))
        time_hash = current_time.hour * 60 + current_time.minute + sum(ord(c) for c in self.event_id)
        rng = np.random.RandomState((seed_val + time_hash) % (2**32 - 1))
        
        noise_s = rng.normal(0, 0.15 + 0.1 * self.intensity)
        noise_e = rng.normal(0, 0.10 + 0.1 * self.intensity)

        stress_gap = max(0.0, current_stress - S_star)
        
        # [配置抽取] 运动降压系数
        gym_relief_base = user.get_param("gym_relief_factor", 0.02)
        relief_factor = gym_relief_base * self.intensity 
        delta_S = -relief_factor * stress_gap * (time_step / 5.0) + noise_s
        
        # [配置抽取 & 耗精提升20%] 
        base_gym_drain = user.get_param("gym_drain_rate", 9.6)
        drain_rate = base_gym_drain * self.intensity / K_resilience
        delta_E = -drain_rate * (time_step / 60.0) + noise_e

        if current_stress + delta_S < S_star - 5.0:
            delta_S = max(delta_S, (S_star - 5.0) - current_stress)
            
        # === 注入运动后燃增益 EPOC ===
        epoc_rate = user.get_param("gym_epoc_rate", 0.05)
        buff_add = (1.5 + 2.0 * self.intensity + epoc_rate) * (time_step / 5.0)
        user.epoc_level = min(100.0, getattr(user, 'epoc_level', 0.0) + buff_add)
            
        return delta_S, delta_E