# event/course_event.py
import math
import os
import csv
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, Optional
from event.base import BaseEvent
from utils.description_score import score_description

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
        
        # 解析非线性 Level
        self.raw_level = level  
        self.level_val = self._parse_level(level) 
        
        # 将参数固化进 metadata，便于前端提取
        if self.metadata is None:
            self.metadata = {}
        self.metadata["credits"] = self.credit
        self.metadata["hours"] = self.hours
        self.metadata["level_str"] = self.raw_level
        self.metadata["level_val"] = self.level_val
        
        # 将课程档案直接追加到 Description 中，确保前端“信息栏”能直接展示出来
        info_badge = f" [课程档案] 学分:{self.credit} | 学时:{self.hours} | 类别:{self.raw_level}(难度{self.level_val})"
        if self.description:
            self.description = f"{self.description}\n{info_badge}"
        else:
            self.description = info_badge

    def _parse_level(self, level) -> float:
        """非线性分类映射器"""
        if isinstance(level, (int, float)):
            return max(1.0, min(5.0, float(level)))
        
        # A-E 字母类别的非线性映射表
        level_map = {
            'C': 5.0,  # 专业必修课：学分高，难度极大
            'B': 4.0,  # 学院公共必修课：基础课，较难
            'D': 3.0,  # 专业选修课：中等难度
            'A': 1.5,  # 学校公共必修课：体育/思政/英语，低认知负荷
            'E': 1.0   # 通识选修课：艺术/社会，极轻松
        }
        
        level_str = str(level).strip().upper()
        return level_map.get(level_str, 2.0) 

    def get_event_type(self) -> str:
        return "course"

    def get_fatigue_weight(self) -> float:
        mapped_weight = 0.85 + (self.level_val - 1.0) * (0.25 / 4.0)
        return round(mapped_weight, 2)
    
    def _compute_cis(self, user, event_start_hour: int) -> float:
        w1 = user.get_param("w1", 0.5)
        w2 = user.get_param("w2", 0.2)
        w3 = user.get_param("w3", 0.3)
        
        # 归一化处理 
        C_norm = self.credit / 6.0
        H_norm = self.hours / 120.0
        L_norm = self.level_val / 5.0  
        
        # 计算加权得分 (0.0 ~ 1.0 之间)
        weighted_score = (w1 * C_norm) + (w2 * H_norm) + (w3 * L_norm)
        
        base = 0.5 + (weighted_score * 1.2)
        
        # 3. 文本情感偏好映射
        desc_score = score_description(self.description, self.course_name)
        F_like = 1.25 - 0.05 * desc_score
        
        # 4. 昼夜生理偏好映射
        time_weights = user.get_param("time_weights", {})
        F_time = 1.0
        for (start_h, end_h), weight in time_weights.items():
            if start_h <= event_start_hour < end_h:
                F_time = weight
                break
                
        return base * F_like * F_time

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        event_start_hour = current_time.hour
        CIS = self._compute_cis(user, event_start_hour)
        
        # 获取睡眠债状态
        sleep_debt = user.get_sleep_debt() 
        f_debt_drain = 1.0 + 0.05 * sleep_debt
        f_debt_stress = 1.0 + 0.04 * sleep_debt
        
        # 昼夜节律惩罚 (凌晨干活惩罚)
        is_circadian_violation = current_time.hour < 6
        f_circadian_drain = 1.4 if is_circadian_violation else 1.0
        f_circadian_stress = 1.2 if is_circadian_violation else 1.0

        # ==========================================
        # Part 1: 精力消耗 (E-Drain)
        # ==========================================
        K_resilience = user.get_param("K_resilience", 1.0)
        base_drain_rate = user.get_param("course_base_drain", 8.0)
        
        linear_drain_rate = (base_drain_rate * CIS) / K_resilience
        
        # 使用非稳态负荷理论的边际耗损代替简单的线性疲劳
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
        
        # 叠加昼夜节律惩罚与非稳态耗能
        delta_E = -linear_drain_rate * f_drain_modifier * f_debt_drain * f_circadian_drain * (time_step / 60.0)

        # ==========================================
        # Part 2: 压力产生 (S-Generation)
        # ==========================================
        D_t = 0.80
        S_star = user.get_param("S_star_init", 50)
        
        # f_s 内部现在已经集成了 get_allostatic_stress_amplifier 阻尼器
        f_s_val = user.course_strategy.f_s(current_stress, current_energy, S_star)
        
        z_awake = user.get_param("Z_awake", 0.5)
        z_factor = user.get_param("Z_factor", 0.5)
        z_raw = z_awake * z_factor
        z_log_mapped = 0.8 + 0.4 * (math.log1p(z_raw) / math.log(2.0))
        
        base_delta_S = D_t * CIS * f_s_val * z_log_mapped * f_debt_stress * f_circadian_stress * (time_step / 5.0)
        
        max_delta = user.course_strategy.get_strategy_max_delta()
        
        if max_delta > 0:
            delta_S = max_delta * math.tanh(base_delta_S / max_delta)
        else:
            delta_S = 0.0
            
        delta_S = max(0.0, delta_S)
        
        return delta_S, delta_E

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds