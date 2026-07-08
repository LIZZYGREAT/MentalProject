# core_engine/state_machine.py
from datetime import datetime
from typing import Tuple, List, Optional
from event.base import BaseEvent
from settings.model_defaults import ACTIVE_NIGHT_STATES, RECOVERY_STATES

class PhysiologyStateMachine:
    """据日程与当前事件判定日/夜/睡眠等状态，并给出睡眠打断惯性增量与睡眠效率。"""
    def __init__(self, schedule: dict, params: dict = None):
        self.t_wake = schedule["wake_time"]
        self.t_sleep_2 = schedule["night_sleep_start"]
        self.prev_state = None
        self.sleep_interruptions = 0
        self.sleep_eff = 1.0
        
        # 挂载全局参数与状态机惩罚字典
        self.params = params or {}
        self.fines_cfg = self.params.get("state_machine_fines", {})

    def determine_state(self, current_time: datetime, has_high_load: bool, routine_ev: Optional[BaseEvent]) -> Tuple[str, float, float, List[str]]:
        """返回状态名、状态切换引起的惯性 dS/dE、以及睡眠相关日志。"""
        logs = []
        
        # 1. 判定当前基准状态
        if has_high_load:
            if current_time < self.t_wake: state = "LATE_NIGHT_ACTIVE"
            elif current_time >= self.t_sleep_2: state = "NIGHT_OVERTIME"
            else: state = "DAY_ACTIVE"
        elif routine_ev:
            if routine_ev.get_event_type() == "sleep":
                state = "RECOVERY_SLEEP" if current_time < self.t_wake else "NIGHT_SLEEP"
            else:
                state = "ROUTINE_MAINTENANCE"
        else:
            if current_time < self.t_wake: state = "LATE_NIGHT_ACTIVE" 
            elif current_time >= self.t_sleep_2: state = "NIGHT_SLEEP" 
            else: state = "DAY_ACTIVE"

        inertia_ds, inertia_de = 0.0, 0.0
        
        # 2. 状态流转惩罚判定 (提取睡眠碎片化惩罚参数)
        ds_pen = self.fines_cfg.get("interrupt_S_penalty", 2.0)
        de_pen = self.fines_cfg.get("interrupt_E_penalty", -5.0)
        
        if self.prev_state in RECOVERY_STATES and state in ACTIVE_NIGHT_STATES:
            self.sleep_interruptions += 1
            logs.append(f"[{current_time.strftime('%H:%M')}] 睡眠被打断 (第 {self.sleep_interruptions} 次)，起夜惩罚 (E{de_pen}, S+{ds_pen})")
            inertia_ds, inertia_de = ds_pen, de_pen
            
        # 3. 睡眠效率折损 (提取折损基线与底座)
        eff_base = self.fines_cfg.get("sleep_eff_base", 0.8)
        eff_drop = self.fines_cfg.get("sleep_eff_drop", 0.1)
        eff_min = self.fines_cfg.get("sleep_eff_min", 0.5)
        
        if self.sleep_interruptions > 0:
            self.sleep_eff = max(eff_min, eff_base - eff_drop * (self.sleep_interruptions - 1))
            if state in RECOVERY_STATES and self.prev_state != state:
                logs.append(f"[{current_time.strftime('%H:%M')}] 碎片化睡眠预警：入睡效率降至 {self.sleep_eff:.2f}")

        self.prev_state = state
        return state, inertia_ds, inertia_de, logs
