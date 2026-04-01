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
        if not hasattr(self, '_cached_user') or self._cached_user is None:
            return 0.85 
            
        task_cfg = self._cached_user.get_param("event_task", {})
        if self.task_type == "exam": return task_cfg.get("T1_exam", 1.1)
        elif self.task_type == "ddl": return task_cfg.get("T2_ddl", 1.05)
        elif self.task_type == "meeting": return task_cfg.get("T3_meeting", 1.0)
        elif self.task_type == "homework": return task_cfg.get("T4_homework", 0.95)
        return task_cfg.get("T5_general", 0.90)

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        self._cached_user = user
        task_cfg = user.get_param("event_task", {})
        
        # === 1. 基础强度判定 ===
        base_intensity = 1.0
        if self.task_type == "exam": base_intensity = task_cfg.get("T1_exam", 1.1)
        elif self.task_type == "ddl": base_intensity = task_cfg.get("T2_ddl", 1.05)
        elif self.task_type == "meeting": base_intensity = task_cfg.get("T3_meeting", 1.0)
        elif self.task_type == "homework": base_intensity = task_cfg.get("T4_homework", 0.95)
        elif self.task_type == "general": base_intensity = task_cfg.get("T5_general", 0.90)
        
        # === 2. 情绪底噪注入 ===
        seed_val = int(user.get_param("random_seed", 42))
        time_hash = current_time.hour * 60 + current_time.minute
        rng = np.random.RandomState((seed_val + time_hash) % (2**32 - 1))
        
        noise_low = task_cfg.get("emotion_noise_low", 0.95)
        noise_high = task_cfg.get("emotion_noise_high", 1.05)
        task_intensity = base_intensity * rng.uniform(noise_low, noise_high)
        
        # === 3. 节律与睡眠债惩罚 ===
        sleep_debt = user.get_sleep_debt()
        penalty_sleep = user.get_param("penalty_sleep_debt", {})
        f_debt_drain = 1.0 + penalty_sleep.get("drain_k", 0.05) * sleep_debt
        f_debt_stress = 1.0 + penalty_sleep.get("stress_k", 0.04) * sleep_debt
        
        penalty_cir = user.get_param("penalty_circadian", {})
        f_circadian_drain = penalty_cir.get("drain_multiplier", 1.4) if current_time.hour < 6 else 1.0
        f_circadian_stress = penalty_cir.get("stress_multiplier", 1.2) if current_time.hour < 6 else 1.0

        # === 4. 计算已持续分钟数 (习惯化衰减的时间轴) ===
        try:
            if isinstance(self.start_time, str):
                st_h, st_m = map(int, self.start_time[-5:].split(':'))
                st_mins = st_h * 60 + st_m
                ct_mins = current_time.hour * 60 + current_time.minute
                elapsed = ct_mins - st_mins
                if elapsed < 0: elapsed += 24 * 60
            else:
                elapsed = (current_time - self.start_time).total_seconds() / 60.0
        except Exception:
            elapsed = 0.0

        # ==========================================
        # Part 1: 精力消耗 (E-Drain)
        # ==========================================
        K_resilience = user.get_param("K_resilience", 1.0)
        base_drain = user.get_param("task_base_drain", 8.5)
        
        linear_drain_rate = (base_drain * task_intensity) / K_resilience
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
            
        delta_E = -linear_drain_rate * f_drain_modifier * f_debt_drain * f_circadian_drain * (time_step / 60.0)

        # ==========================================
        # Part 2: 压力产生 (S-Generation)
        # ==========================================
        D_t = user.get_param("D_t_task", 0.65)
        S_star = user.get_param("S_star_init", 50.0)
        
        f_s_val = user.course_strategy.f_s(current_stress, current_energy, S_star)
        
        z_awake = user.get_param("Z_awake", 0.5)
        z_factor = user.get_param("Z_factor", 0.5)
        z_raw = z_awake * z_factor
        z_log_mapped = 0.8 + 0.4 * (math.log1p(z_raw) / math.log(2.0))
        
        # [核心机制] 提取习惯化配置并计算衰减乘数 Theta(t)
        habit_cfg = user.get_param("habituation_params", {})
        mu = habit_cfg.get("floor_mu_task", 0.35)  # 注意：Task 使用 0.35 专属底线
        t_half = habit_cfg.get("t_half_hyperbolic", 90.0)
        theta_t = mu + (1.0 - mu) * (t_half / (t_half + max(0.0, elapsed)))
        
        # 将 Theta(t) 挂载到基准增量中
        base_delta_S = D_t * task_intensity * f_s_val * z_log_mapped * f_debt_stress * f_circadian_stress * theta_t * (time_step / 5.0)
        
        max_delta = user.course_strategy.get_strategy_max_delta()
        if base_delta_S > max_delta:
            delta_S = max_delta + math.log1p(base_delta_S - max_delta)
        else:
            delta_S = base_delta_S
            
        return max(0.0, delta_S), delta_E