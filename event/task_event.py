"""Task event DTO for CTSSM event assessment."""

from typing import Any, Dict

from event.base import BaseEvent
from settings.model_defaults import DEFAULT_TASK_TYPE


class TaskEvent(BaseEvent):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        name: str = "",
        description: str = "",
        task_type: str = DEFAULT_TASK_TYPE,
        metadata: Dict[str, Any] = None,
    ) -> None:
        super().__init__(event_id, start_time, end_time, name, description, metadata)
        self.task_type = task_type

    def get_event_type(self) -> str:
        return "task"
