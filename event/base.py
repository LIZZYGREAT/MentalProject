"""Event data objects consumed by the production CTSSM."""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
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
        self._start_dt = None
        self._end_dt = None

    def get_start_datetime(self) -> datetime:
        if self._start_dt is None:
            self._start_dt = datetime.strptime(self.start_time, "%H:%M")
        return self._start_dt

    def get_end_datetime(self) -> datetime:
        if self._end_dt is None:
            self._end_dt = datetime.strptime(self.end_time, "%H:%M")
        return self._end_dt

    def is_active_at(self, current_time: datetime) -> bool:
        start = self.get_start_datetime()
        end = self.get_end_datetime()
        current = current_time.replace(
            year=start.year,
            month=start.month,
            day=start.day,
        )
        if end < start:
            end += timedelta(days=1)
            if current.hour < start.hour:
                current += timedelta(days=1)
        return start <= current < end

    @abstractmethod
    def get_event_type(self) -> str:
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:
        item = {
            "event_id": self.event_id,
            "type": self.get_event_type(),
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "description": self.description,
            "metadata": self.metadata,
        }
        if hasattr(self, "task_type"):
            item["task_type"] = getattr(self, "task_type")
        return item
