# utils/event_factory.py
import re
from typing import List, Dict, Any
from event.base import BaseEvent
from event.course_event import CourseEvent
from event.rest_event import RestEvent, MealEvent, NapEvent
from event.task_event import TaskEvent
from event.gym_event import GymEvent
from event.library_event import LibraryEvent
from entry.class_info_data import CLASS_INFO_DICT
from settings.event_routing import COURSE_HINT_PATTERN, ROUTINE_PATTERNS, TASK_PATTERNS
from settings.model_defaults import DEFAULT_EVENT_END, DEFAULT_EVENT_START, DEFAULT_UNKNOWN_EVENT_NAME

class EventFactory:
    """将飞书/Agent 的 JSON 行映射为 Course/Task/Gym/Library/Rest/Meal/Nap 等具体事件类。"""
    @staticmethod
    def create_from_json(events_data: List[Dict[str, Any]]) -> List[BaseEvent]:
        """
        参数 events_data: 每项含 id/summary/start_time/end_time/description，可选 event_type 强制类型。
        路由顺序：显式 event_type -> 名称关键词（餐/睡/健身/自习…）-> 考试 ddl 等 -> 课表字典或「课」-> 默认 general 任务。
        返回: BaseEvent 实例列表。
        """
        events = []
        for idx, data in enumerate(events_data):
            ev_id = data.get("id", f"ev_{idx}")
            name = data.get("summary", data.get("name", DEFAULT_UNKNOWN_EVENT_NAME))
            desc = data.get("description", "")
            st = data.get("start_time", DEFAULT_EVENT_START)
            et = data.get("end_time", DEFAULT_EVENT_END)
            ev_type_hint = data.get("event_type", "").lower()
            
            # 1. 强力提示优先 (通常来自沙盒强行注入)
            if ev_type_hint == "course":
                events.append(CourseEvent(ev_id, st, et, name=name, description=desc))
                continue
            elif ev_type_hint == "rest":
                events.append(RestEvent(ev_id, st, et, name=name, description=desc))
                continue
            elif ev_type_hint == "gym":
                events.append(GymEvent(ev_id, st, et, name=name, description=desc))
                continue
            elif ev_type_hint == "library":
                events.append(LibraryEvent(ev_id, st, et, name=name, description=desc))
                continue
                
            # 2. NLP 智能路由：基于日程名称的正则特征分类
            name_lower = name.lower()
            
            # --- a. 生态与生理事件 ---
            if re.search(ROUTINE_PATTERNS["meal"], name_lower):
                events.append(MealEvent(ev_id, st, et, name=name, description=desc))
                continue
            if re.search(ROUTINE_PATTERNS["nap"], name_lower):
                events.append(NapEvent(ev_id, st, et, name=name, description=desc))
                continue
            if re.search(ROUTINE_PATTERNS["gym"], name_lower):
                events.append(GymEvent(ev_id, st, et, name=name, description=desc))
                continue
            if re.search(ROUTINE_PATTERNS["library"], name_lower):
                events.append(LibraryEvent(ev_id, st, et, name=name, description=desc))
                continue
                
            # --- b. 广义高压任务 (TaskEvent - 五级梯队字典) ---
            # T1
            if re.search(TASK_PATTERNS["exam"], name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="exam"))
                continue
            # T2
            if re.search(TASK_PATTERNS["ddl"], name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="ddl"))
                continue
            # T3
            if re.search(TASK_PATTERNS["meeting"], name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="meeting"))
                continue
            # T4
            if re.search(TASK_PATTERNS["homework"], name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="homework"))
                continue
                
            # --- c. 传统授课 (CourseEvent) ---
            if name in CLASS_INFO_DICT or re.search(COURSE_HINT_PATTERN, name):
                events.append(CourseEvent(ev_id, st, et, name=name, description=desc))
                continue
                
            # --- d. 兜底策略 (T5) ---
            events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="general"))
            
        return events
