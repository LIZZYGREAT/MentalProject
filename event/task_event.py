# event/task_event.py
from datetime import datetime
from typing import Dict, Any, Tuple
import math
import numpy as np
import re
from event.base import BaseEvent
from algorithm.high_load import HighLoadProfile, calculate_high_load_impact
from settings.model_defaults import DEFAULT_INITIAL_ENERGY, DEFAULT_TASK_TYPE, DEFAULT_TASK_WEIGHT

class TaskEvent(BaseEvent):
    """非课程高压事务：用 task_type 定疲劳权重与 metadata，压力链与课程同形。"""
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", 
                 task_type: str = DEFAULT_TASK_TYPE, metadata: Dict[str, Any] = None):
        """
        参数 task_type: exam/ddl/meeting/homework/general 等，影响 get_fatigue_weight。
        """
        super().__init__(event_id, start_time, end_time, name, description, metadata)
        self.task_type = task_type
        
    def get_event_type(self) -> str:
        return "task"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        """仅返回 dS；精力由 dual 计算。"""
        time_step = user.get_param("time_step", 5)
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, DEFAULT_INITIAL_ENERGY, current_time, time_step)
        return ds

    def get_fatigue_weight(self) -> float:
        """按任务类型返回疲劳权重，供连续负荷与马尔可夫特征使用。"""
        if not hasattr(self, '_cached_user') or self._cached_user is None:
            return DEFAULT_TASK_WEIGHT
            
        task_cfg = self._cached_user.get_param("event_task", {})
        if self.task_type == "exam": return task_cfg.get("weight_exam", task_cfg.get("T1_exam", 1.10))
        elif self.task_type == "ddl": return task_cfg.get("weight_ddl", task_cfg.get("T2_ddl", 1.05))
        elif self.task_type == "meeting": return task_cfg.get("weight_meeting", task_cfg.get("T3_meeting", 0.85))
        elif self.task_type == "homework": return task_cfg.get("weight_homework", task_cfg.get("T4_homework", 0.95))
        return task_cfg.get("weight_general", task_cfg.get("T5_general", DEFAULT_TASK_WEIGHT))

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int, 
                                   step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                                   is_substep: bool = False) -> Tuple[float, float]:
        """
        [接口修改]：透传步长锁定随机因子 step_noise_s。
        """
        self._cached_user = user
        
        if not is_substep:
            self.metadata["detail"] = f"类型:{self.task_type}|E:{current_energy:.1f}"
            self.metadata["weight_factor"] = f"({self.get_fatigue_weight():.2f})"

        task_weight = self.get_fatigue_weight()
        profile = HighLoadProfile(
            load_weight=task_weight,
            d_t_key="D_t_task",
            d_t_default=0.55,
            base_drain_key="task_base_drain",
            base_drain_default=5.0,
            habit_floor_key="floor_mu_task",
            trace_label="W_task",
        )
        delta_S, delta_E, trace = calculate_high_load_impact(
            user=user,
            current_stress=current_stress,
            current_energy=current_energy,
            current_time=current_time,
            time_step=time_step,
            event_start_time=self.start_time,
            profile=profile,
            step_noise_s=step_noise_s,
        )

        if not is_substep:
            if "math_trace" not in self.metadata:
                trace_str = (
                    f"$$S_{{dot}} = D_t({trace['D_t']:.2f}) \\cdot W_{{task}}({task_weight:.2f}) \\cdot f_s({trace['f_s']:.2f}) "
                    f"\\cdot Z_{{env}}({trace['Z_env']:.2f}) \\cdot \\Theta({trace['Theta']:.2f}) \\cdot f_{{debtS}}({trace['f_debt_stress']:.2f}) = {trace['S_dot']:.3f}$$<br>"
                    f"$$\\Delta S = S_{{dot}} \\cdot \\left(\\frac{{{time_step}}}{{5.0}}\\right) = {delta_S:.3f}$$<br>"
                    f"$$E_{{dot}} = -\\frac{{Base({trace['base_drain']:.1f}) \\cdot W_{{task}}({task_weight:.2f}) \\cdot Acc({trace['acc_multiplier']:.2f})}}{{K_{{res}}({trace['K_resilience']:.1f})}} "
                    f"\\cdot f_{{drain}}({trace['f_drain_modifier']:.2f}) \\cdot f_{{debt}}({trace['f_debt_drain']:.2f}) \\cdot f_{{cir}}({trace['f_circadian_drain']:.2f})$$<br>"
                    f"$$\\Delta E = E_{{dot}} \\cdot \\left(\\frac{{{time_step}}}{{60.0}}\\right) = {delta_E:.3f}$$"
                )
                self.metadata["math_trace"] = trace_str

        return delta_S, delta_E
