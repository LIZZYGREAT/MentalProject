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

class EventFactory:
    """
    智能事件装配工厂：将飞书泛型日程动态路由到精确的领域模型
    """
    @staticmethod
    def create_from_json(events_data: List[Dict[str, Any]]) -> List[BaseEvent]:
        events = []
        for idx, data in enumerate(events_data):
            ev_id = data.get("id", f"ev_{idx}")
            name = data.get("summary", data.get("name", "未知事件"))
            desc = data.get("description", "")
            st = data.get("start_time", "08:00")
            et = data.get("end_time", "09:00")
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
            if re.search(r"饭|餐|食堂|breakfast|lunch|dinner", name_lower):
                events.append(MealEvent(ev_id, st, et, name=name, description=desc))
                continue
            if re.search(r"午休|睡觉|打盹|nap|sleep", name_lower):
                events.append(NapEvent(ev_id, st, et, name=name, description=desc))
                continue
            if re.search(r"健身|锻炼|跑步|游泳|gym|workout", name_lower):
                events.append(GymEvent(ev_id, st, et, name=name, description=desc))
                continue
            if re.search(r"自习|图书馆|复习|library|study", name_lower):
                events.append(LibraryEvent(ev_id, st, et, name=name, description=desc))
                continue
                
            # --- b. 广义高压任务 (TaskEvent - 五级梯队字典) ---
            # T1
            if re.search(r"考|测验|期末|期中|竞赛|比赛|面试|答辩", name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="exam"))
                continue
            # T2
            if re.search(r"ddl|截止|提交|汇报|大作业|实验|攻关", name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="ddl"))
                continue
            # T3
            if re.search(r"会|讨论|例会|面谈|讲座|编程", name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="meeting"))
                continue
            # T4
            if re.search(r"作业|报告|项目|练习|培训", name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="homework"))
                continue
                
            # --- c. 传统授课 (CourseEvent) ---
            if name in CLASS_INFO_DICT or "课" in name:
                events.append(CourseEvent(ev_id, st, et, name=name, description=desc))
                continue
                
            # --- d. 兜底策略 (T5) ---
            events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="general"))
            
        return events