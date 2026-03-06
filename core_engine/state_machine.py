# core_engine/state_machine.py
from datetime import datetime
from typing import Tuple, List, Optional
from event.base import BaseEvent

class PhysiologyStateMachine:
    def __init__(self, schedule: dict):
        self.t_wake = schedule["wake_time"]
        self.t_sleep_2 = schedule["night_sleep_start"]
        self.prev_state = None
        self.sleep_interruptions = 0
        self.sleep_eff = 1.0

    def determine_state(self, current_time: datetime, has_high_load: bool, routine_ev: Optional[BaseEvent]) -> Tuple[str, float, float, List[str]]:
        """返回：当前状态, 突发S增量(如起夜), 突发E增量, 日志列表"""
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
        
        # 2. 状态流转惩罚判定 (睡眠打断)
        if self.prev_state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"] and state in ["LATE_NIGHT_ACTIVE", "NIGHT_OVERTIME"]:
            self.sleep_interruptions += 1
            logs.append(f"[{current_time.strftime('%H:%M')}] ⏰ 睡眠被打断 (第 {self.sleep_interruptions} 次)！触发起夜开机惩罚 (E-5, S+2)")
            inertia_ds, inertia_de = 2.0, -5.0
            
        # 3. 睡眠效率折损
        if self.sleep_interruptions > 0:
            self.sleep_eff = max(0.5, 0.8 - 0.1 * (self.sleep_interruptions - 1))
            if state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"] and self.prev_state not in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
                logs.append(f"[{current_time.strftime('%H:%M')}] ⚠️ [生理机制] 睡眠碎片化：恢复效率降至 {self.sleep_eff*100:.0f}%")

        if state != self.prev_state:
            if self.prev_state is not None:
                logs.append(f"[{current_time.strftime('%H:%M')}] 🔄 状态流转: {self.prev_state} -> {state}")
            self.prev_state = state

        return state, inertia_ds, inertia_de, logs