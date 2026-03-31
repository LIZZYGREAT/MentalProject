# event/task_event.py
from datetime import datetime
from typing import Dict, Any, Tuple
import math
import numpy as np
import re
from event.base import BaseEvent

class TaskEvent(BaseEvent):
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", 
                 task_type: str = "general", metadata: Dict[str, Any] = None):
        super().__init__(event_id, start_time, end_time, name, description, metadata)
        self.task_type = task_type
        
    def get_event_type(self) -> str:
        return "task"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds

    def get_fatigue_weight(self) -> float:
        """
        [配置抽取] 依据 task_type 返回特有疲劳权重，用于计算连续负荷时长
        """
        # 如果获取不到用户实例，提供兜底基线
        if not hasattr(self, '_cached_user') or self._cached_user is None:
            return 0.85 
            
        task_cfg = self._cached_user.get_param("event_task", {})
        mapping = {
            "exam": task_cfg.get("T1_exam", 1.1),
            "ddl": task_cfg.get("T2_ddl", 1.05),
            "meeting": task_cfg.get("T3_meeting", 1.0),
            "homework": task_cfg.get("T4_homework", 0.95),
            "general": task_cfg.get("T5_general", 0.90)
        }
        return mapping.get(self.task_type, task_cfg.get("T5_general", 0.90))
        
    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        self._cached_user = user 
        
        # === Part 0: 读取基准强度与确定性状态微调 ===
        intensity_map = user.get_param("task_base_intensities", {})
        base_intensity = intensity_map.get(self.task_type, intensity_map.get("general", 0.90))
        
        seed_val = int(user.get_param("random_seed", 42))
        time_hash = current_time.hour * 60 + current_time.minute + sum(ord(c) for c in self.event_id)
        rng = np.random.RandomState((seed_val + time_hash) % (2**32 - 1))
        
        # [配置抽取] 情绪底噪字典
        task_cfg = user.get_param("event_task", {})
        emotion_noise_low = task_cfg.get("emotion_noise_low", 0.95)
        emotion_noise_high = task_cfg.get("emotion_noise_high", 1.05)
        
        emotion_multiplier = rng.uniform(emotion_noise_low, emotion_noise_high)
        task_intensity = base_intensity * emotion_multiplier

        # [配置抽取] 统一获取惩罚参数
        penalty_sleep = user.get_param("penalty_sleep_debt", {})
        penalty_circadian = user.get_param("penalty_circadian", {})

        sleep_debt = user.get_sleep_debt() 
        f_debt_drain = 1.0 + penalty_sleep.get("drain_k", 0.05) * sleep_debt
        f_debt_stress = 1.0 + penalty_sleep.get("stress_k", 0.04) * sleep_debt

        is_circadian_violation = current_time.hour < 6
        f_circadian_drain = penalty_circadian.get("drain_multiplier", 1.4) if is_circadian_violation else 1.0
        f_circadian_stress = penalty_circadian.get("stress_multiplier", 1.2) if is_circadian_violation else 1.0
        
        # === Part 1: 精力消耗 (E-Drain) ===
        K_resilience = user.get_param("K_resilience", 1.0)
        base_drain = user.get_param("task_base_drain", 8.5)
        
        linear_drain_rate = (base_drain * task_intensity) / K_resilience
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
            
        delta_E = -linear_drain_rate * f_drain_modifier * f_debt_drain * f_circadian_drain * (time_step / 60.0)

        # === Part 2: 压力产生 (S-Generation) ===
        D_t = user.get_param("D_t_task", 0.65)
        S_star = user.get_param("S_star_init", 50.0)
        
        f_s_val = user.course_strategy.f_s(current_stress, current_energy, S_star)
        
        z_awake = user.get_param("Z_awake", 0.5)
        z_factor = user.get_param("Z_factor", 0.5)
        z_raw = z_awake * z_factor
        z_log_mapped = 0.8 + 0.4 * (math.log1p(z_raw) / math.log(2.0))
        
        base_delta_S = D_t * task_intensity * f_s_val * z_log_mapped * f_debt_stress * f_circadian_stress * (time_step / 5.0)
        
        max_delta = user.course_strategy.get_strategy_max_delta()
        
        if max_delta > 0:
            delta_S = max_delta * math.tanh(base_delta_S / max_delta)
        else:
            delta_S = 0.0
            
        delta_S = max(0.0, delta_S)
        
        return delta_S, delta_E