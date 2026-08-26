"""Fail-closed propagation for the bounded cross-day Forecast dependency graph."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING
import uuid
from zoneinfo import ZoneInfo

from app.repositories import ForecastSnapshotRepository, WarningScheduleRepository

if TYPE_CHECKING:
    from app.services.forecast_coordinator import ForecastCoordinator


logger = logging.getLogger(__name__)


class ForecastDependencyRefreshService:
    """Own the two supported dependency edges: yesterday→today and today→tomorrow."""

    def __init__(
        self,
        forecasts: ForecastSnapshotRepository,
        warnings: WarningScheduleRepository,
        coordinator: "ForecastCoordinator",
        *,
        timezone_name: str,
    ) -> None:
        self.forecasts = forecasts
        self.warnings = warnings
        self.coordinator = coordinator
        self.timezone = ZoneInfo(timezone_name)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[tuple[uuid.UUID, date], asyncio.Task[None]] = {}
        self._requested: dict[tuple[uuid.UUID, date], tuple[int, str]] = {}
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("forecast dependency refresh service is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("forecast dependency refresh service is already bound")
        self._loop = loop

    def dependent_date(self, source_date: date) -> date | None:
        today = datetime.now(self.timezone).date()
        if source_date in {today - timedelta(days=1), today}:
            return source_date + timedelta(days=1)
        return None

    def invalidate_dependent_now(
        self,
        participant_id: uuid.UUID,
        source_date: date,
        *,
        reason: str,
    ) -> dict[str, int]:
        dependent = self.dependent_date(source_date)
        if dependent is None:
            return {"forecasts_invalidated": 0, "warnings_cancelled": 0}
        return self.forecasts.invalidate_current_for_date(
            self.warnings,
            participant_id,
            dependent,
            reason=reason,
        )

    async def refresh_dependent_after_source(
        self,
        participant_id: uuid.UUID,
        source_date: date,
        *,
        reason: str,
    ) -> dict | None:
        dependent = self.dependent_date(source_date)
        if dependent is None:
            return None
        return await self.coordinator.ensure_forecast(
            participant_id,
            dependent,
            reason,
            refresh_calendar=False,
            force_followup=True,
        )

    def enqueue_dependent_after_source(
        self,
        participant_id: uuid.UUID,
        source_date: date,
        *,
        reason: str,
    ) -> bool:
        dependent = self.dependent_date(source_date)
        loop = self._loop
        if dependent is None:
            return False
        if loop is None or loop.is_closed() or self._closed:
            logger.error(
                "forecast_dependency_refresh_not_running participant_id=%s source_date=%s",
                participant_id,
                source_date,
            )
            return False
        loop.call_soon_threadsafe(
            self._enqueue,
            participant_id,
            source_date,
            reason,
        )
        return True

    def _enqueue(
        self,
        participant_id: uuid.UUID,
        source_date: date,
        reason: str,
    ) -> None:
        if self._closed:
            return
        key = (participant_id, source_date)
        requested, _previous_reason = self._requested.get(key, (0, reason))
        self._requested[key] = (requested + 1, reason)
        if key in self._tasks:
            return

        async def run() -> None:
            processed = 0
            try:
                while not self._closed:
                    requested, current_reason = self._requested.get(
                        key, (0, reason)
                    )
                    if requested <= processed:
                        break
                    try:
                        await self.refresh_dependent_after_source(
                            participant_id,
                            source_date,
                            reason=current_reason,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "forecast_dependency_refresh_failed "
                            "participant_id=%s source_date=%s",
                            participant_id,
                            source_date,
                        )
                    processed = requested
            except asyncio.CancelledError:
                raise
            finally:
                self._tasks.pop(key, None)
                if self._requested.get(key, (0, reason))[0] <= processed:
                    self._requested.pop(key, None)

        self._tasks[key] = asyncio.create_task(
            run(),
            name=(
                f"forecast-dependency-refresh-{participant_id}-"
                f"{source_date.isoformat()}"
            ),
        )

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._requested.clear()
