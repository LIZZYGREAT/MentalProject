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
    """面授课程：用 CIS 强度刻画负荷，结合 f_s 与习惯化 Theta(t) 产生 dS，线性 drain 产生 dE。"""
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", 
                 course_name: str = "", credit: float = None, 
                 hours: float = None, level=None, 
                 metadata: Dict[str, Any] = None):
        """
        参数:
            course_name: 优先用于查 class_info_data 得学分/学时/等级；缺省则用 credit/hours/level 或默认；
            description/name: 参与文本情感得 F_like，影响 CIS。
        """
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
        self.level_str = level
        self.metadata["credits"] = self.credit
        self.metadata["hours"] = self.hours
        self.metadata["level_str"] = self.level_str

        self.desc_score = score_description(self.description)
        if self.desc_score == 5.0 and self.name:
            self.desc_score = score_description(self.name)
        self.F_like = convert_score_to_Flike(self.desc_score)
        
        try:
            if isinstance(start_time, str):
                st_time_str = start_time.split(' ')[-1]
                self.start_hour = int(st_time_str.split(':')[0])
            else:
                self.start_hour = start_time.hour
        except Exception:
            self.start_hour = 8

        self.credit_count = self.credit / 16.0 

    def get_event_type(self) -> str:
        return "course"
        
    def get_fatigue_weight(self) -> float:
        """无 user 缓存时返回 1.0；否则等于 CIS（与连续负荷、马尔可夫强度一致）。"""
        if not hasattr(self, '_cached_user') or self._cached_user is None:
            return 1.0
        return self._calculate_cis_score(self._cached_user)

    def _calculate_cis_score(self, user) -> float:
        """
        课程强度指数 CIS：w1*课业密度 + w2*时段权重 + w3*偏好，再按 F_like 与 lambda_like 缩放，并截断到 [0.5,2.5]。
        """
        w1 = user.get_param("w1", 0.4)
        w2 = user.get_param("w2", 0.2)
        w3 = user.get_param("w3", 0.3)
        lambda_like = user.get_param("lambda_like", 0.25)
        
        L_value = 1.0 if self.level_str in ['A', 'A+'] else (0.8 if self.level_str in ['B', 'B+'] else 0.5)
        
        # 引入平方根压缩，破除线性学分/学时比率导致的维度爆炸
        ratio = self.credit / max(1.0, self.hours)
        I_basic = math.sqrt(ratio) * L_value * 2.5
        
        time_weights = user.get_param("time_weights", {})
        T_weight = 1.0
        for (start_h, end_h), weight in time_weights.items():
            if start_h <= self.start_hour < end_h:
                T_weight = weight
                break
                
        time_prefs = user.get_time_preferences()
        P_weight = 1.0
        if "morning_person" in time_prefs and self.start_hour < 10:
            P_weight = 0.85
        elif "night_owl" in time_prefs and self.start_hour >= 18:
            P_weight = 0.85
        
        cis_score = w1 * I_basic + w2 * T_weight + w3 * P_weight
        
        if self.F_like > 0:
            cis_score *= (1.0 - lambda_like * self.F_like)
        else:
            cis_score *= (1.0 + lambda_like * abs(self.F_like))
            
        return max(0.5, min(2.5, cis_score))

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        """兼容接口：内部用 time_step 调 dual 仅取 dS。"""
        time_step = user.get_param("time_step", 5)
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, time_step)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int, 
                                   step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                                   is_substep: bool = False) -> Tuple[float, float]:

        self._cached_user = user
        
        cis_score = self._calculate_cis_score(user)
        if not is_substep:
            self.metadata["detail"] = f"CIS:{cis_score:.1f}|Flike:{self.F_like:.1f}|E:{current_energy:.1f}"
            self.metadata["weight_factor"] = f"({self.get_fatigue_weight():.2f})"

        K_resilience = user.get_param("K_resilience", 1.0)
        # [修改点] 修复键名，读取真实的 7.5 基准耗能
        base_course_drain = user.get_param("course_base_drain", 7.5)
        
        f_debt_drain = 1.0 + 0.1 * user.get_sleep_debt()
        f_circadian_drain = 1.0
        if current_time.hour >= 22 or current_time.hour < 6:
            f_circadian_drain = 1.5
            
        # [修改点] 将经过时间 t_elapsed 提前计算，用于生成疲劳加速系数
        try:
            if isinstance(self.start_time, str):
                st_time_str = self.start_time.split(' ')[-1]
                if len(st_time_str.split(':')) == 3: st_time_str = st_time_str[:5]
                st_dt = datetime.strptime(f"{current_time.strftime('%Y-%m-%d')} {st_time_str}", "%Y-%m-%d %H:%M")
            else:
                st_dt = self.start_time
            t_elapsed = (current_time - st_dt).total_seconds() / 60.0
            t_elapsed = max(0.0, t_elapsed)
        except Exception:
            t_elapsed = 0.0

        # 每小时增加一定比例的精力消耗
        fatigue_acc_k = user.get_param("fatigue_acceleration_k", 0.15)
        acc_multiplier = 1.0 + fatigue_acc_k * (t_elapsed / 60.0)
        
        # 叠加加速系数计算线性消耗率
        linear_drain_rate = (base_course_drain * cis_score * acc_multiplier) / K_resilience
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
        
        delta_E = -linear_drain_rate * f_drain_modifier * f_debt_drain * f_circadian_drain * (time_step / 60.0)

        D_t = user.get_param("D_t_course", 0.80)
        S_star = user.get_param("S_star_init", 50.0)
        
        f_s_val = user.course_strategy.f_s(current_stress, current_energy, S_star, step_noise_s=step_noise_s)
        
        z_awake = user.get_param("Z_awake", 0.5)
        z_factor = user.get_param("Z_factor", 0.5)
        z_raw = z_awake * z_factor
        z_log_mapped = 0.8 + 0.4 * (math.log1p(z_raw) / math.log(2.0))
        
        habit_cfg = user.get_param("habituation_params", {})
        mu = habit_cfg.get("floor_mu_course", 0.40)
        t_half = habit_cfg.get("t_half_hyperbolic", 90.0)

        Theta_t = mu + (1.0 - mu) * (t_half / (t_half + t_elapsed))
        
        S_dot = f_s_val * cis_score * D_t * z_log_mapped * Theta_t
        delta_S = S_dot * (time_step / 5.0)

        if not is_substep:
            if "math_trace" not in self.metadata:
                trace_str = (
                    f"$$S_{{dot}} = D_t({D_t:.2f}) \\cdot CIS({cis_score:.2f}) \\cdot f_s({f_s_val:.2f}) "
                    f"\\cdot Z_{{env}}({z_log_mapped:.2f}) \\cdot \\Theta({Theta_t:.2f}) = {S_dot:.3f}$$<br>"
                    f"$$\\Delta S = S_{{dot}} \\cdot \\left(\\frac{{{time_step}}}{{5.0}}\\right) = {delta_S:.3f}$$<br>"
                    f"$$E_{{dot}} = -\\frac{{Base({base_course_drain:.1f}) \\cdot CIS({cis_score:.2f}) \\cdot Acc({acc_multiplier:.2f})}}{{K_{{res}}({K_resilience:.1f})}} "
                    f"\\cdot f_{{drain}}({f_drain_modifier:.2f}) \\cdot f_{{debt}}({f_debt_drain:.2f}) \\cdot f_{{cir}}({f_circadian_drain:.2f})$$<br>"
                    f"$$\\Delta E = E_{{dot}} \\cdot \\left(\\frac{{{time_step}}}{{60.0}}\\right) = {delta_E:.3f}$$"
                )
                self.metadata["math_trace"] = trace_str

        return delta_S, delta_E