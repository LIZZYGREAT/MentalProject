# event/task_event.py
from datetime import datetime
from typing import Dict, Any, Tuple
import math
import numpy as np
import re
from event.base import BaseEvent

class TaskEvent(BaseEvent):
    """
    非课程类高压任务事件
    已接入：
    1. 睡眠债惩罚机制：没睡好会导致任务期间精力掉得更快，压力升得更高。
    2. 昼夜节律惩罚 (Circadian Penalty)：凌晨干活(00:00-06:00)耗能1.4倍，增压1.2倍。
    3. T1-T5 负荷自动映射：根据任务类型自动匹配基础强度。
    4. 数值防爆修正与软截断。
    """
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", 
                 task_type: str = "general", metadata: Dict[str, Any] = None):
        super().__init__(event_id, start_time, end_time, name, description, metadata)
        self.task_type = task_type
        
    def get_event_type(self) -> str:
        return "task"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        """实现旧接口兼容性"""
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds

    def get_fatigue_weight(self) -> float:
        """
        [核心分级] 疲劳折算权重，用于 Solver 计算连续负荷时长
        """
        mapping = {
            "exam": 1.1,      # T1 极限负荷 (考试/比赛)
            "ddl": 1.05,      # T2 重度负荷 (DDL/提交)
            "meeting": 0.95,  # T3 中度被动 (会议/讲座)
            "homework": 0.9,  # T4 轻度输出 (作业/编程)
            "general": 0.85   # T5 兜底日常
        }
        return mapping.get(self.task_type, 0.85)
        
    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        
        # === Part 0: 读取基准强度与确定性状态微调 ===
        intensity_map = user.get_param("task_base_intensities", {
            "exam": 1.1, "ddl": 1.0, "meeting": 0.9, "homework": 0.8, "general": 0.7
        })
        base_intensity = intensity_map.get(self.task_type, intensity_map.get("general", 0.7))
        
        seed_val = int(user.get_param("random_seed", 42))
        time_hash = current_time.hour * 60 + current_time.minute + sum(ord(c) for c in self.event_id)
        rng = np.random.RandomState((seed_val + time_hash) % (2**32 - 1))
        
        emotion_multiplier = rng.uniform(0.95, 1.05)
        task_intensity = base_intensity * emotion_multiplier

        # === [睡眠债惩罚接入] ===
        sleep_debt = user.get_sleep_debt() 
        f_debt_drain = 1.0 + 0.05 * sleep_debt
        f_debt_stress = 1.0 + 0.04 * sleep_debt

        # === [新增：昼夜节律惩罚 (Circadian Penalty)] ===
        is_circadian_violation = current_time.hour < 6
        f_circadian_drain = 1.4 if is_circadian_violation else 1.0
        f_circadian_stress = 1.2 if is_circadian_violation else 1.0
        
        # === Part 1: 精力消耗 (E-Drain) ===
        K_resilience = user.get_param("K_resilience", 1.0)
        fatigue_accel = user.get_param("fatigue_acceleration", 1.25)
        
        # [配置抽取]
        base_drain = user.get_param("task_base_drain", 8.5)
        linear_drain_rate = (base_drain * task_intensity) / K_resilience
        
        energy_ratio = max(0.0, min(100.0, current_energy)) / 100.0
        f_fatigue = 1.0 + fatigue_accel * math.pow(1.0 - energy_ratio, 1.5)
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
            
        # 融入昼夜节律与睡眠债双重惩罚
        delta_E = -linear_drain_rate * f_fatigue * f_drain_modifier * f_debt_drain * f_circadian_drain * (time_step / 60.0)

        # === Part 2: 压力产生 (S-Generation) ===
        D_t = 0.65
        S_star = user.get_param("S_star_init", 50)
        
        f_s_val = user.course_strategy.f_s(current_stress, current_energy, S_star)
        
        z_awake = user.get_param("Z_awake", 0.5)
        z_factor = user.get_param("Z_factor", 0.5)
        z_raw = z_awake * z_factor
        z_log_mapped = 0.8 + 0.4 * (math.log1p(z_raw) / math.log(2.0))
        
        # 融入昼夜节律惩罚
        base_delta_S = D_t * task_intensity * f_s_val * z_log_mapped * f_debt_stress * f_circadian_stress * (time_step / 5.0)
        
        max_delta = user.course_strategy.get_strategy_max_delta()
        
        if max_delta > 0:
            delta_S = max_delta * math.tanh(base_delta_S / max_delta)
        else:
            delta_S = 0.0
            
        delta_S = max(0.0, delta_S)
        
        return delta_S, delta_E