# utils/event_factory.py
import re
from typing import List, Dict, Any
from event.base import BaseEvent
from event.course_event import CourseEvent
from event.rest_event import RestEvent, MealEvent, NapEvent, SleepEvent
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
        # Calendar providers may return the same instance twice (for example,
        # through overlapping calendar pages).  Exact duplicates must not be
        # interpreted as simultaneous independent stressors.
        seen_instances = set()
        for idx, data in enumerate(events_data):
            ev_id = data.get("id", f"ev_{idx}")
            name = data.get("summary", data.get("name", DEFAULT_UNKNOWN_EVENT_NAME))
            desc = data.get("description", "")
            st = data.get("start_time", DEFAULT_EVENT_START)
            et = data.get("end_time", DEFAULT_EVENT_END)
            instance_key = (
                str(name or "").strip().casefold(),
                str(st or "").strip(),
                str(et or "").strip(),
                str(data.get("date") or "").strip(),
                str(desc or "").strip().casefold(),
                str(data.get("event_type") or "").strip().lower(),
                str(data.get("task_type") or data.get("level") or "").strip().lower(),
            )
            if instance_key in seen_instances:
                continue
            seen_instances.add(instance_key)
            ev_type_hint = data.get("event_type", "").lower()
            raw_metadata = data.get("metadata")
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            # Preserve theory-facing fields supplied by clients.  They may be
            # nested (preferred) or flat for backward-compatible integrations.
            for key in (
                "objective",
                "appraisal",
                "recovery",
                "deadline",
                "social_evaluation",
                "uncontrollability",
                "control",
                "cognitive_demand",
                "physical_demand",
                "novelty",
                "unfinished",
                "threat",
                "challenge",
                "importance",
                "uncertainty",
                "expected_effort",
                "rumination",
                "status",
                "event_status",
                "cancelled",
                "cancelled_at",
                "canceled_at",
                "kernel_mode",
                "semantic_inference",
                "external_semantic_inference",
                "semantic_context",
                "lifecycle",
                "completion_policy",
                "outcome_status",
            ):
                if key in data and key not in metadata:
                    metadata[key] = data[key]
            if "task_type" in data and "task_type" not in metadata:
                metadata["task_type"] = data["task_type"]
            
            # 1. 强力提示优先 (通常来自沙盒强行注入)
            if ev_type_hint == "course":
                events.append(
                    CourseEvent(
                        ev_id,
                        st,
                        et,
                        name=name,
                        description=desc,
                        course_name=data.get("course_name", name),
                        credit=data.get("credit", data.get("credits")),
                        hours=data.get("hours"),
                        level=data.get("level"),
                        metadata=metadata,
                    )
                )
                continue
            elif ev_type_hint == "task":
                events.append(
                    TaskEvent(
                        ev_id,
                        st,
                        et,
                        name=name,
                        description=desc,
                        task_type=data.get("task_type", data.get("level", "general")),
                        metadata=metadata,
                    )
                )
                continue
            elif ev_type_hint == "rest":
                events.append(RestEvent(ev_id, st, et, name=name, description=desc, metadata=metadata))
                continue
            elif ev_type_hint == "meal":
                events.append(MealEvent(ev_id, st, et, name=name, description=desc, metadata=metadata))
                continue
            elif ev_type_hint == "nap":
                events.append(NapEvent(ev_id, st, et, name=name, description=desc, metadata=metadata))
                continue
            elif ev_type_hint == "sleep":
                events.append(SleepEvent(ev_id, st, et, name=name, description=desc, metadata=metadata))
                continue
            elif ev_type_hint == "gym":
                events.append(
                    GymEvent(
                        ev_id,
                        st,
                        et,
                        name=name,
                        description=desc,
                        intensity=data.get("intensity", 0.7),
                        metadata=metadata,
                    )
                )
                continue
            elif ev_type_hint == "library":
                events.append(
                    LibraryEvent(
                        ev_id,
                        st,
                        et,
                        name=name,
                        description=desc,
                        study_intensity=data.get("study_intensity", data.get("intensity", 0.7)),
                        metadata=metadata,
                    )
                )
                continue
                
            # 2. NLP 智能路由：基于日程名称的正则特征分类
            name_lower = name.lower()
            
            # --- a. 生态与生理事件 ---
            if re.search(ROUTINE_PATTERNS["meal"], name_lower):
                events.append(MealEvent(ev_id, st, et, name=name, description=desc, metadata=metadata))
                continue
            if re.search(ROUTINE_PATTERNS["nap"], name_lower):
                events.append(NapEvent(ev_id, st, et, name=name, description=desc, metadata=metadata))
                continue
            if re.search(ROUTINE_PATTERNS["gym"], name_lower):
                events.append(
                    GymEvent(
                        ev_id,
                        st,
                        et,
                        name=name,
                        description=desc,
                        intensity=data.get("intensity", 0.7),
                        metadata=metadata,
                    )
                )
                continue
            if re.search(ROUTINE_PATTERNS["library"], name_lower):
                events.append(
                    LibraryEvent(
                        ev_id,
                        st,
                        et,
                        name=name,
                        description=desc,
                        study_intensity=data.get("study_intensity", data.get("intensity", 0.7)),
                        metadata=metadata,
                    )
                )
                continue
                
            # --- b. 广义高压任务 (TaskEvent - 五级梯队字典) ---
            # T1
            if re.search(TASK_PATTERNS["exam"], name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="exam", metadata=metadata))
                continue
            # T2
            if re.search(TASK_PATTERNS["ddl"], name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="ddl", metadata=metadata))
                continue
            # T3
            if re.search(TASK_PATTERNS["meeting"], name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="meeting", metadata=metadata))
                continue
            # T4
            if re.search(TASK_PATTERNS["homework"], name_lower):
                events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="homework", metadata=metadata))
                continue
                
            # --- c. 传统授课 (CourseEvent) ---
            if name in CLASS_INFO_DICT or re.search(COURSE_HINT_PATTERN, name):
                events.append(CourseEvent(ev_id, st, et, name=name, description=desc, metadata=metadata))
                continue
                
            # --- d. 兜底策略 (T5) ---
            events.append(TaskEvent(ev_id, st, et, name=name, description=desc, task_type="general", metadata=metadata))
            
        return events
