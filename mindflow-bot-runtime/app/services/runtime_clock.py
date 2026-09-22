"""Small injectable wall clock for runtime-local date semantics."""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable
from zoneinfo import ZoneInfo


class RuntimeClock:
    """Return a configured-timezone view of the current time."""

    def __init__(
        self,
        timezone_name: str,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.timezone = ZoneInfo(timezone_name)
        self._now_fn = now_fn

    def now(self) -> datetime:
        value = self._now_fn() if self._now_fn is not None else datetime.now(self.timezone)
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def local_date(self) -> date:
        return self.now().date()
