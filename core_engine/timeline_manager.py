# core_engine/timeline_manager.py
from datetime import datetime
from typing import List, Dict, Optional
from event.base import BaseEvent

class TimelineManager:
    def __init__(self, events: List[BaseEvent], date_str: str):
        self.events = events
        self.date_str = date_str
        self.base_date = datetime.strptime(date_str, "%Y-%m-%d")

    def _parse_time_robust(self, time_obj, current_time: datetime) -> datetime:
        if isinstance(time_obj, str):
            t_str = time_obj.split(' ')[-1]
            if len(t_str.split(':')) == 3:
                t_str = t_str[:5] 
            return datetime.strptime(f"{self.date_str} {t_str}", "%Y-%m-%d %H:%M")
        else:
            return time_obj.replace(year=current_time.year, month=current_time.month, day=current_time.day)

    def analyze_schedule(self) -> Dict[str, datetime]:
        """提取作息边界：何时起床，何时入睡，熬夜到何时"""
        sleep_events = [e for e in self.events if e.get_event_type() == "sleep"]
        morning_sleeps, night_sleeps = [], []
        
        for se in sleep_events:
            st_str = se.start_time if isinstance(se.start_time, str) else se.start_time.strftime("%H:%M")
            if ' ' in st_str: st_str = st_str.split(' ')[-1]
            if int(st_str.split(':')[0]) < 12: morning_sleeps.append(se)
            else: night_sleeps.append(se)
        
        actual_wake_time = self.base_date.replace(hour=7, minute=30)
        if morning_sleeps:
            last_se = max(morning_sleeps, key=lambda x: x.end_time if not isinstance(x.end_time, str) else x.end_time)
            et = last_se.end_time if isinstance(last_se.end_time, str) else last_se.end_time.strftime("%H:%M")
            if ' ' in et: et = et.split(' ')[-1]
            actual_wake_time = datetime.strptime(f"{self.date_str} {et}", "%Y-%m-%d %H:%M")
            
        actual_sleep_start = self.base_date.replace(hour=23, minute=30)
        if night_sleeps:
            first_se = min(night_sleeps, key=lambda x: x.start_time if not isinstance(x.start_time, str) else x.start_time)
            st = first_se.start_time if isinstance(first_se.start_time, str) else first_se.start_time.strftime("%H:%M")
            if ' ' in st: st = st.split(' ')[-1]
            actual_sleep_start = datetime.strptime(f"{self.date_str} {st}", "%Y-%m-%d %H:%M")

        late_night_active_end = self.base_date
        active_loads = [e for e in self.events if e.get_event_type() in ["course", "task", "gym", "library"]]
        for al in active_loads:
            et_str = al.end_time if isinstance(al.end_time, str) else al.end_time.strftime("%H:%M")
            if ' ' in et_str: et_str = et_str.split(' ')[-1]
            dt_et = datetime.strptime(f"{self.date_str} {et_str}", "%Y-%m-%d %H:%M")
            if dt_et <= actual_wake_time:
                late_night_active_end = max(late_night_active_end, dt_et)

        return {
            "late_night_active_end": late_night_active_end,
            "wake_time": actual_wake_time,
            "night_sleep_start": actual_sleep_start
        }

    def get_active_high_load_events(self, current_time: datetime) -> List[BaseEvent]:
        active = []
        for ev in self.events:
            if ev.get_event_type() not in ["course", "task", "gym", "library"]: continue 
            try:
                s_dt = self._parse_time_robust(ev.start_time, current_time)
                e_dt = self._parse_time_robust(ev.end_time, current_time)
                if s_dt <= current_time < e_dt: active.append(ev)
            except Exception: continue
        return active

    def get_active_routine(self, current_time: datetime) -> Optional[BaseEvent]:
        for ev in self.events:
            if ev.get_event_type() in ["meal", "nap", "sleep", "rest"]: 
                try:
                    s_dt = self._parse_time_robust(ev.start_time, current_time)
                    e_dt = self._parse_time_robust(ev.end_time, current_time)
                    if s_dt <= current_time < e_dt: return ev
                except Exception: continue
        return None