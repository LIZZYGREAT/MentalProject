"""Course event DTO for CTSSM event assessment."""

from typing import Any, Dict

from entry.class_info_data import CLASS_INFO_DICT
from event.base import BaseEvent
from settings.model_defaults import DEFAULT_COURSE_PROFILE


class CourseEvent(BaseEvent):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        name: str = "",
        description: str = "",
        course_name: str = "",
        credit: float = None,
        hours: float = None,
        level=None,
        metadata: Dict[str, Any] = None,
    ) -> None:
        super().__init__(event_id, start_time, end_time, name, description, metadata)
        self.course_name = course_name or name
        course = CLASS_INFO_DICT.get(self.course_name, {})
        self.credit = float(
            credit
            if credit is not None
            else course.get("credits", DEFAULT_COURSE_PROFILE["credits"])
        )
        self.hours = float(
            hours
            if hours is not None
            else course.get("hours", DEFAULT_COURSE_PROFILE["hours"])
        )
        self.level_str = (
            level
            if level is not None
            else course.get("level", DEFAULT_COURSE_PROFILE["level"])
        )
        self.metadata["credits"] = self.credit
        self.metadata["hours"] = self.hours
        self.metadata["level_str"] = self.level_str

    def get_event_type(self) -> str:
        return "course"
