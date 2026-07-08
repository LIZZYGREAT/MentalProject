# utils/routine_weaver.py
from datetime import datetime
from typing import List, Tuple
from event.base import BaseEvent
from event.rest_event import MealEvent, NapEvent, SleepEvent
from algorithm.time_utils import minutes_to_hhmm, time_to_minutes
from settings.model_defaults import DEFAULT_SLEEP_TIME, DEFAULT_WAKE_TIME, HIGH_LOAD_EVENT_TYPES

class RoutineWeaver:
    """
    智能影子日程编织器 (Routine Weaver)
    负责：熬夜判定、起夜判定、弹性睡眠目标计算、生成真实的 SleepEvent、以及智能贪婪寻找午休缝隙。
    """
    def __init__(self, user):
        self.user = user
        self.cfg = self.user.get_param("routine_weaver", {})
        
        self.default_wake_min = self._time_to_mins(self.user.get_param("default_wake_time", DEFAULT_WAKE_TIME))
        self.default_sleep_min = self._time_to_mins(self.user.get_param("default_sleep_time", DEFAULT_SLEEP_TIME))
        
        self.max_delay_wake_min = self._time_to_mins(self.cfg.get("max_delay_wake_time", "11:00")) 
        self.ideal_sleep_hours = self.cfg.get("ideal_sleep_hours", 8.0)

    def _time_to_mins(self, time_str: str) -> int:
        """Convert configured or event time to minutes from midnight."""
        return time_to_minutes(time_str)

    def _mins_to_str(self, mins: int) -> str:
        """Convert minutes from midnight to normalized HH:MM."""
        return minutes_to_hhmm(mins)

    def _get_occupied_blocks(self, events: List[BaseEvent]) -> List[Tuple[int, int]]:
        occupied_blocks = []
        for ev in events:
            if ev.get_event_type() not in HIGH_LOAD_EVENT_TYPES:
                continue
            try:
                s_min = self._time_to_mins(ev.start_time)
                e_min = self._time_to_mins(ev.end_time)
                if e_min <= s_min:
                    occupied_blocks.append((s_min, 1440))
                    occupied_blocks.append((0, e_min))
                else:
                    occupied_blocks.append((s_min, e_min))
            except Exception:
                continue
        
        occupied_blocks.sort()
        merged = []
        for blk in occupied_blocks:
            if not merged:
                merged.append(blk)
            else:
                last_s, last_e = merged[-1]
                if blk[0] < last_e:
                    merged[-1] = (last_s, max(last_e, blk[1]))
                else:
                    merged.append(blk)
        return merged

    def _get_free_gaps(self, bound_s: int, bound_e: int, occupied_blocks: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        gaps = []
        curr = bound_s
        for bs, be in occupied_blocks:
            if bs > curr and curr < bound_e:
                gaps.append((curr, min(bs, bound_e)))
            curr = max(curr, be)
            if curr >= bound_e:
                break
        if curr < bound_e:
            gaps.append((curr, bound_e))
        return gaps

    def _inject_sleep_events(self, events: List[BaseEvent], occupied_blocks: List[Tuple[int, int]]) -> List[BaseEvent]:
        final_events = list(events)
        
        late_night_end = 0
        for bs, be in occupied_blocks:
            if bs < self.default_wake_min:
                late_night_end = max(late_night_end, be)
                
        real_wake_min = self.default_wake_min
        late_hours = 0.0
        
        if late_night_end > 0:
            late_hours = late_night_end / 60.0
            ideal_wake = late_night_end + int((self.ideal_sleep_hours + late_hours * 0.25) * 60)
            
            next_event_start = 1440
            for bs, be in occupied_blocks:
                if bs >= late_night_end:
                    next_event_start = min(next_event_start, bs)
                    break
            
            next_event_buffer = self.cfg.get("next_event_buffer", 30)
            real_wake_min = min(ideal_wake, next_event_start - next_event_buffer, self.max_delay_wake_min)
            real_wake_min = max(real_wake_min, late_night_end)

        actual_sleep_mins = 0
        morning_gaps = self._get_free_gaps(0, real_wake_min, occupied_blocks)
        transition_buffer = self.cfg.get("sleep_transition_buffer", 15)
        ignore_before = self.cfg.get("ignore_early_midnight_before", 150)
        for gs, ge in morning_gaps:
            if gs == 0 and ge < ignore_before:
                continue 
                
            actual_start = gs
            if gs > 0:
                actual_start = gs + transition_buffer
            
            if actual_start < ge:
                actual_sleep_mins += (ge - actual_start)
                final_events.append(SleepEvent(
                    f"sleep_morning_{actual_start}", 
                    self._mins_to_str(actual_start), 
                    self._mins_to_str(ge), 
                    name="夜间/晨间睡眠"
                ))
                
        target_sleep_mins = (self.ideal_sleep_hours + late_hours * 0.25) * 60
        sleep_debt_mins = max(0, target_sleep_mins - actual_sleep_mins)
        self.user.set_sleep_debt(sleep_debt_mins / 60.0)

        night_gaps = self._get_free_gaps(self.default_sleep_min, 1439, occupied_blocks)
        for gs, ge in night_gaps:
            actual_start = gs
            if gs > self.default_sleep_min: 
                actual_start = gs + transition_buffer
                
            if actual_start < ge:
                final_events.append(SleepEvent(
                    f"sleep_night_{actual_start}", 
                    self._mins_to_str(actual_start), 
                    self._mins_to_str(ge), 
                    name="夜间入睡"
                ))

        return final_events

    def _find_best_slot(self, bound_s: int, bound_e: int, ideal_s: int, ideal_e: int, 
                       min_dur: int, occupied_blocks: List[Tuple[int, int]]) -> Tuple[int, int]:
        gaps = self._get_free_gaps(bound_s, bound_e, occupied_blocks)
        best_slot = None
        best_score = -999999
        ideal_dur = ideal_e - ideal_s
        
        for gs, ge in gaps:
            L = ge - gs
            if L < min_dur: continue
            if gs <= ideal_s and ge >= ideal_e:
                return (ideal_s, ideal_e)
                
            block_len = min(L, ideal_dur)
            possible_s = ideal_s
            if possible_s < gs:
                possible_s = gs
            elif possible_s + block_len > ge:
                possible_s = ge - block_len
                
            score = block_len * 1000 - abs(possible_s - ideal_s)
            if score > best_score:
                best_score = score
                best_slot = (possible_s, possible_s + block_len)
        return best_slot

    def inject_routine_events(self, events: List[BaseEvent], date_str: str) -> List[BaseEvent]:
        """在已有日程上插入睡眠、午餐、午睡、晚餐等例行块。"""
        occupied_blocks = self._get_occupied_blocks(events)
        final_events = self._inject_sleep_events(events, occupied_blocks)
        all_blocks = self._get_occupied_blocks(final_events)

        # 1. 注入午餐 (动态读取时空边界)
        lunch_b_s = self._time_to_mins(self.cfg.get("lunch_window_start", "11:00"))
        lunch_b_e = self._time_to_mins(self.cfg.get("lunch_window_end", "13:30"))
        lunch_i_s = self._time_to_mins(self.cfg.get("lunch_ideal_start", "11:40"))
        lunch_i_e = self._time_to_mins(self.cfg.get("lunch_ideal_end", "12:20"))
        
        lunch_slot = self._find_best_slot(
            bound_s=lunch_b_s, bound_e=lunch_b_e, 
            ideal_s=lunch_i_s, ideal_e=lunch_i_e, 
            min_dur=self.cfg.get("meal_min_duration", 20), occupied_blocks=all_blocks
        )
        lunch_end_min = 12*60
        if lunch_slot:
            ls, le = lunch_slot
            lunch_end_min = le
            dur = le - ls
            m_type = "normal" if dur >= self.cfg.get("meal_normal_min_duration", 30) else "rushed"
            final_events.append(MealEvent("meal_lunch", self._mins_to_str(ls), self._mins_to_str(le), meal_type=m_type, name="午餐"))

        # 2. 注入午睡 (动态读取阈值)
        sleep_debt = self.user.get_sleep_debt()
        debt_thresh = self.cfg.get("nap_debt_threshold", 0.5)
        ideal_nap_dur = self.cfg.get("nap_ideal_debt", 90) if sleep_debt > debt_thresh else self.cfg.get("nap_ideal_normal", 40)
        min_nap_dur = self.cfg.get("nap_min_debt", 20) if sleep_debt > debt_thresh else self.cfg.get("nap_min_normal", 15)
        
        nap_delay = self.cfg.get("nap_delay_after_lunch", 10)
        nap_bound_s = lunch_end_min + nap_delay
        nap_bound_e = self._time_to_mins(self.cfg.get("nap_window_end", "13:50"))
        
        nap_slot = self._find_best_slot(
            bound_s=nap_bound_s, bound_e=nap_bound_e,
            ideal_s=nap_bound_s, ideal_e=nap_bound_s + ideal_nap_dur,
            min_dur=min_nap_dur, occupied_blocks=all_blocks
        )
        if nap_slot:
            ns, ne = nap_slot
            dur = ne - ns
            n_type = "proper" if dur >= 30 else "short"
            metadata = {"nap_type": n_type, "is_repaying_debt": True if sleep_debt > debt_thresh and dur > 30 else False}
            final_events.append(NapEvent("nap_noon", self._mins_to_str(ns), self._mins_to_str(ne), nap_type=n_type, name="午睡", metadata=metadata))

        # 3. 注入晚餐 (动态读取时空边界)
        dinner_b_s = self._time_to_mins(self.cfg.get("dinner_window_start", "17:00"))
        dinner_b_e = self._time_to_mins(self.cfg.get("dinner_window_end", "19:30"))
        dinner_i_s = self._time_to_mins(self.cfg.get("dinner_ideal_start", "17:40"))
        dinner_i_e = self._time_to_mins(self.cfg.get("dinner_ideal_end", "18:30"))
        
        dinner_slot = self._find_best_slot(
            bound_s=dinner_b_s, bound_e=dinner_b_e,
            ideal_s=dinner_i_s, ideal_e=dinner_i_e,
            min_dur=self.cfg.get("meal_min_duration", 20), occupied_blocks=all_blocks
        )
        if dinner_slot:
            ds, de = dinner_slot
            dur = de - ds
            m_type = "normal" if dur >= self.cfg.get("meal_normal_min_duration", 30) else "rushed"
            final_events.append(MealEvent("meal_dinner", self._mins_to_str(ds), self._mins_to_str(de), meal_type=m_type, name="晚餐"))

        return final_events
