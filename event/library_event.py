"""Library event DTO for CTSSM event assessment."""

from typing import Any, Dict

from event.base import BaseEvent


class LibraryEvent(BaseEvent):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        name: str = "自习",
        description: str = "",
        study_intensity: float = 0.7,
        metadata: Dict[str, Any] = None,
    ) -> None:
        super().__init__(event_id, start_time, end_time, name, description, metadata)
        self.provided_intensity = max(0.1, min(1.0, float(study_intensity)))

    def get_event_type(self) -> str:
        return "library"
