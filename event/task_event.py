# event/task_event.py
from datetime import datetime
from typing import Dict, Any, Tuple
import math
import numpy as np
import re
from event.base import BaseEvent

class TaskEvent(BaseEvent):
    """非课程高压事务：用 task_type 定疲劳权重与 metadata，压力链与课程同形。"""
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", 
                 task_type: str = "general", metadata: Dict[str, Any] = None):
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
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, time_step)
        return ds

    def get_fatigue_weight(self) -> float:
        """按任务类型返回疲劳权重，供连续负荷与马尔可夫特征使用。"""
        if not hasattr(self, '_cached_user') or self._cached_user is None:
            return 0.85 
            
        task_cfg = self._cached_user.get_param("event_task", {})
        if self.task_type == "exam": return task_cfg.get("weight_exam", 1.5)
        elif self.task_type == "ddl": return task_cfg.get("weight_ddl", 1.2)
        elif self.task_type == "meeting": return task_cfg.get("weight_meeting", 0.6)
        elif self.task_type == "homework": return task_cfg.get("weight_homework", 0.8)
        return task_cfg.get("weight_general", 0.85)

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

        K_resilience = user.get_param("K_resilience", 1.0)
        base_task_drain = user.get_param("base_task_drain", 3.0) 
        task_weight = self.get_fatigue_weight()
        
        f_debt_drain = 1.0 + 0.1 * user.get_sleep_debt()
        f_circadian_drain = 1.0
        if current_time.hour >= 22 or current_time.hour < 6:
            f_circadian_drain = 1.5
            
        linear_drain_rate = (base_task_drain * task_weight) / K_resilience
        
        f_drain_modifier = 1.0
        if hasattr(user.course_strategy, 'get_energy_drain_modifier'):
            f_drain_modifier = user.course_strategy.get_energy_drain_modifier(current_energy)
            
        delta_E = -linear_drain_rate * f_drain_modifier * f_debt_drain * f_circadian_drain * (time_step / 60.0)

        D_t = user.get_param("D_t_task", 0.65)
        S_star = user.get_param("S_star_init", 50.0)
        
        f_s_val = user.course_strategy.f_s(current_stress, current_energy, S_star, step_noise_s=step_noise_s)
        
        z_awake = user.get_param("Z_awake", 0.5)
        z_factor = user.get_param("Z_factor", 0.5)
        z_raw = z_awake * z_factor
        z_log_mapped = 0.8 + 0.4 * (math.log1p(z_raw) / math.log(2.0))
        
        habit_cfg = user.get_param("habituation_params", {})
        mu = habit_cfg.get("floor_mu_task", 0.35)  
        t_half = habit_cfg.get("t_half_hyperbolic", 90.0)
        
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

        Theta_t = mu + (1.0 - mu) * (t_half / (t_half + t_elapsed))
        
        S_dot = f_s_val * task_weight * D_t * z_log_mapped * Theta_t
        delta_S = S_dot * (time_step / 5.0)

        if not is_substep:
            if "math_trace" not in self.metadata:
                trace_str = (
                    f"$$S_{{dot}} = D_t({D_t:.2f}) \\cdot W_{{task}}({task_weight:.2f}) \\cdot f_s({f_s_val:.2f}) "
                    f"\\cdot Z_{{env}}({z_log_mapped:.2f}) \\cdot \\Theta({Theta_t:.2f}) = {S_dot:.3f}$$<br>"
                    f"$$\\Delta S = S_{{dot}} \\cdot \\left(\\frac{{{time_step}}}{{5.0}}\\right) = {delta_S:.3f}$$<br>"
                    f"$$E_{{dot}} = -\\frac{{Base({base_task_drain:.1f}) \\cdot W_{{task}}({task_weight:.2f})}}{{K_{{res}}({K_resilience:.1f})}} "
                    f"\\cdot f_{{drain}}({f_drain_modifier:.2f}) \\cdot f_{{debt}}({f_debt_drain:.2f}) \\cdot f_{{cir}}({f_circadian_drain:.2f})$$<br>"
                    f"$$\\Delta E = E_{{dot}} \\cdot \\left(\\frac{{{time_step}}}{{60.0}}\\right) = {delta_E:.3f}$$"
                )
                self.metadata["math_trace"] = trace_str

        return delta_S, delta_E