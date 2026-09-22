"""Event data objects consumed by the production CTSSM."""

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseEvent(ABC):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        name: str = "",
        description: str = "",
        metadata: Dict[str, Any] = None,
    ) -> None:
        self.event_id = event_id
        self.start_time = start_time
        self.end_time = end_time
        self.name = name
        self.description = description
        self.metadata = metadata or {}

    @abstractmethod
    def get_event_type(self) -> str:
        raise NotImplementedError
