"""Gym event DTO for CTSSM event assessment."""

from typing import Any, Dict

from event.base import BaseEvent


class GymEvent(BaseEvent):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        name: str = "运动",
        description: str = "",
        intensity: float = 0.7,
        metadata: Dict[str, Any] = None,
    ) -> None:
        super().__init__(event_id, start_time, end_time, name, description, metadata)
        self.intensity = max(0.1, min(1.0, float(intensity)))
        self.metadata["detail"] = f"强度: {self.intensity:.1f}"

    def get_event_type(self) -> str:
        return "gym"
