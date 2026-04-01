# event/course_event.py
import math
import os
import csv
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, Optional
from event.base import BaseEvent
from utils.description_score import score_description, convert_score_to_Flike

from entry.class_info_data import CLASS_INFO_DICT
class_info_dict = CLASS_INFO_DICT

class CourseEvent(BaseEvent):
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", 
                 course_name: str = "", credit: float = None, 
                 hours: float = None, level=None, 
                 metadata: Dict[str, Any] = None):
        super().__init__(event_id, start_time, end_time, name, description, metadata)
        self.course_name = course_name or name
        
        if self.course_name and self.course_name in class_info_dict:
            course_data = class_info_dict[self.course_name]
            if credit is None: credit = course_data.get('credits', 2.5)
            if hours is None: hours = course_data.get('hours', 60.0)
            if level is None: level = course_data.get('level', 'C') 
        else:
            if credit is None: credit = 2.5
            if hours is None: hours = 60.0
            if level is None: level = 'C'

        self.credit = float(credit)
        self.hours = float(hours)
        self.level = str(level).upper()
        self.metadata["credits"] = self.credit
        self.metadata["hours"] = self.hours
        self.metadata["level_str"] = self.level
        self._cached_user = None

    def get_event_type(self) -> str:
        return "course"
        
    def _compute_cis(self, user) -> float:
        level_multipliers = {'A': 1.5, 'B': 1.2, 'C': 1.0, 'D': 0.8}
        L_i = level_multipliers.get(self.level, 1.0)
        
        w1 = user.get_param("w1", 0.4)
        w2 = user.get_param("w2", 0.2)
        w3 = user.get_param("w3", 0.3)
        lambda_like = user.get_param("lambda_like", 0.25)
        
        norm_credit = min(1.0, self.credit / 5.0)
        norm_hours = min(1.0, self.hours / 120.0)
        
        score = score_description(self.description, self.name)
        F_like = convert_score_to_Flike(score)
        
        self.metadata["F_like"] = F_like
        
        CIS = w1 * norm_credit + w2 * norm_hours + w3 * L_i
        CIS *= (1.0 - lambda_like * F_like)
        return CIS

    def _get_time_weight(self, current_time: datetime, user) -> float:
        hour = current_time.hour
        time_weights = user.get_param("time_weights", {})
        for (start, end), weight in time_weights.items():
            if start <= hour < end:
                return weight
        return 1.0

    def get_fatigue_weight(self) -> float:
        if not self._cached_user:
            return 1.0
        CIS = self._compute_cis(self._cached_user)
        base_weight = self._cached_user.get_param("cognitive_weight", 0.65)
        return min(2.0, CIS / base_weight)

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        self._cached_user = user
        CIS = self._compute_cis(user)
        
        time_weight = self._get_time_weight(current_time, user)
        CIS *= time_weight
        
        sleep_debt = user.get_sleep_debt()
        penalty_sleep = user.get_param("penalty_sleep_debt", {})
        f_debt_drain = 1.0 + penalty_sleep.get("drain_k", 0.05) * sleep_debt
        f_debt_stress = 1.0 + penalty_sleep.get("stress_k", 0.04) * sleep_debt
        
        penalty_cir = user.get_param("penalty_circadian", {})
        f_circadian_drain = penalty_cir.get("drain_multiplier", 1.4) if current_time.hour < 6 else 1.0
        f_circadian_stress = penalty_cir.get("stress_multiplier", 1.2) if current_time.hour < 6 else 1.0
        
        # 计算已持续的分钟数以支持习惯化衰减
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
        base_drain = user.get_param("course_base_drain", 8.5)
        linear_drain_rate = (base_drain * CIS) / K_resilience
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
        
        delta_E = -linear_drain_rate * f_drain_modifier * f_debt_drain * f_circadian_drain * (time_step / 60.0)

        # ==========================================
        # Part 2: 压力产生 (S-Generation)
        # ==========================================
        D_t = user.get_param("D_t_course", 0.80)
        S_star = user.get_param("S_star_init", 50.0)
        
        f_s_val = user.course_strategy.f_s(current_stress, current_energy, S_star)
        
        z_awake = user.get_param("Z_awake", 0.5)
        z_factor = user.get_param("Z_factor", 0.5)
        z_raw = z_awake * z_factor
        z_log_mapped = 0.8 + 0.4 * (math.log1p(z_raw) / math.log(2.0))
        
        # 获取习惯化配置并计算衰减乘数 Theta(t)
        habit_cfg = user.get_param("habituation_params", {})
        mu = habit_cfg.get("floor_mu_course", 0.40)
        t_half = habit_cfg.get("t_half_hyperbolic", 90.0)
        theta_t = mu + (1.0 - mu) * (t_half / (t_half + max(0.0, elapsed)))
        
        base_delta_S = D_t * CIS * f_s_val * z_log_mapped * f_debt_stress * f_circadian_stress * theta_t * (time_step / 5.0)
        
        max_delta = user.course_strategy.get_strategy_max_delta()
        if base_delta_S > max_delta:
            delta_S = max_delta + math.log1p(base_delta_S - max_delta)
        else:
            delta_S = base_delta_S
            
        return max(0.0, delta_S), delta_E