"""Recovery event DTOs for CTSSM event assessment."""

from typing import Any, Dict

from event.base import BaseEvent


class RestEvent(BaseEvent):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        name: str = "",
        description: str = "",
        metadata: Dict[str, Any] = None,
    ) -> None:
        super().__init__(event_id, start_time, end_time, name, description, metadata)

    def get_event_type(self) -> str:
        return "rest"


class MealEvent(BaseEvent):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        meal_type: str = "normal",
        name: str = "就餐",
        description: str = "",
        metadata: Dict[str, Any] = None,
    ) -> None:
        meta = dict(metadata or {})
        meta["meal_type"] = meal_type
        super().__init__(event_id, start_time, end_time, name, description, meta)
        self.meal_type = meal_type

    def get_event_type(self) -> str:
        return "meal"


class NapEvent(BaseEvent):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        nap_type: str = "proper",
        name: str = "午睡",
        description: str = "",
        metadata: Dict[str, Any] = None,
    ) -> None:
        meta = dict(metadata or {})
        meta["nap_type"] = nap_type
        super().__init__(event_id, start_time, end_time, name, description, meta)
        self.nap_type = nap_type

    def get_event_type(self) -> str:
        return "nap"


class SleepEvent(BaseEvent):
    def __init__(
        self,
        event_id: str,
        start_time: str,
        end_time: str,
        name: str = "睡眠",
        description: str = "",
        metadata: Dict[str, Any] = None,
    ) -> None:
        super().__init__(event_id, start_time, end_time, name, description, metadata)

    def get_event_type(self) -> str:
        return "sleep"
