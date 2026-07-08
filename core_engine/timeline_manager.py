# core_engine/timeline_manager.py
from datetime import datetime
from typing import List, Dict, Optional
from event.base import BaseEvent
from algorithm.time_utils import extract_hhmm, parse_datetime_on_date, time_to_minutes
from settings.model_defaults import (
    DEFAULT_WAKE_TIME,
    DEFAULT_SLEEP_TIME,
    HIGH_LOAD_EVENT_TYPES,
    ROUTINE_EVENT_TYPES,
)

class TimelineManager:
    """封装单日事件列表，提供作息边界解析与当前时刻活跃事件查询。"""
    def __init__(self, events: List[BaseEvent], date_str: str):
        self.events = events
        self.date_str = date_str
        self.base_date = datetime.strptime(date_str, "%Y-%m-%d")

    def _parse_time_robust(self, time_obj, current_time: datetime) -> datetime:
        """将事件的 start/end 转为当日 datetime，供区间包含判断。"""
        return parse_datetime_on_date(time_obj, self.date_str)

    def analyze_schedule(self) -> Dict[str, datetime]:
        """
        从 sleep 事件推断 wake_time、night_sleep_start；再由高负荷事件结束时刻得 late_night_active_end。
        返回键: late_night_active_end, wake_time, night_sleep_start（均为当日 datetime）。
        """
        sleep_events = [e for e in self.events if e.get_event_type() == "sleep"]
        morning_sleeps, night_sleeps = [], []
        
        for se in sleep_events:
            if time_to_minutes(se.start_time) < 12 * 60:
                morning_sleeps.append(se)
            else:
                night_sleeps.append(se)
        
        wake_h, wake_m = map(int, DEFAULT_WAKE_TIME.split(":"))
        actual_wake_time = self.base_date.replace(hour=wake_h, minute=wake_m)
        if morning_sleeps:
            last_se = max(morning_sleeps, key=lambda x: time_to_minutes(x.end_time))
            actual_wake_time = parse_datetime_on_date(last_se.end_time, self.date_str)
            
        sleep_h, sleep_m = map(int, DEFAULT_SLEEP_TIME.split(":"))
        actual_sleep_start = self.base_date.replace(hour=sleep_h, minute=sleep_m)
        if night_sleeps:
            first_se = min(night_sleeps, key=lambda x: time_to_minutes(x.start_time))
            actual_sleep_start = parse_datetime_on_date(first_se.start_time, self.date_str)

        late_night_active_end = self.base_date
        active_loads = [e for e in self.events if e.get_event_type() in HIGH_LOAD_EVENT_TYPES]
        for al in active_loads:
            dt_et = parse_datetime_on_date(al.end_time, self.date_str)
            if dt_et <= actual_wake_time:
                late_night_active_end = max(late_night_active_end, dt_et)

        return {
            "late_night_active_end": late_night_active_end,
            "wake_time": actual_wake_time,
            "night_sleep_start": actual_sleep_start
        }

    def get_active_high_load_events(self, current_time: datetime) -> List[BaseEvent]:
        """当前时刻处于区间内的 course/task/gym/library 事件列表。"""
        active = []
        for ev in self.events:
            if ev.get_event_type() not in HIGH_LOAD_EVENT_TYPES: continue
            try:
                s_dt = self._parse_time_robust(ev.start_time, current_time)
                e_dt = self._parse_time_robust(ev.end_time, current_time)
                if s_dt <= current_time < e_dt: active.append(ev)
            except Exception: continue
        return active

    def get_active_routine(self, current_time: datetime) -> Optional[BaseEvent]:
        """当前时刻处于区间内的 meal/nap/sleep/rest 之一，若无则 None。"""
        for ev in self.events:
            if ev.get_event_type() in ROUTINE_EVENT_TYPES:
                try:
                    s_dt = self._parse_time_robust(ev.start_time, current_time)
                    e_dt = self._parse_time_robust(ev.end_time, current_time)
                    if s_dt <= current_time < e_dt: return ev
                except Exception: continue
        return None
