"""Forecast invalidation and managed recompute after an Observation commit."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import logging
import uuid
from zoneinfo import ZoneInfo

from app.repositories import ForecastSnapshotRepository, WarningScheduleRepository
from app.services.forecast_coordinator import ForecastCoordinator


logger = logging.getLogger(__name__)


class ObservationForecastRefreshService:
    """Fail closed immediately, then coalesce recomputes on the runtime loop."""

    def __init__(
        self,
        forecasts: ForecastSnapshotRepository,
        warnings: WarningScheduleRepository,
        coordinator: ForecastCoordinator,
        *,
        timezone_name: str,
    ) -> None:
        self.forecasts = forecasts
        self.warnings = warnings
        self.coordinator = coordinator
        self.timezone = ZoneInfo(timezone_name)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[tuple[uuid.UUID, date], asyncio.Task[None]] = {}
        self._requested: dict[tuple[uuid.UUID, date], int] = {}
        self._closed = False

    def start(self) -> None:
        """Bind task creation to the running application event loop."""

        if self._closed:
            raise RuntimeError("observation forecast refresh service is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("observation forecast refresh service is already bound")
        self._loop = loop

    def on_observation_committed(
        self,
        *,
        participant_id: uuid.UUID,
        observed_at: datetime,
        created: bool,
    ) -> dict[str, int]:
        if not created:
            return {"forecasts_invalidated": 0, "warnings_cancelled": 0}
        local_date = observed_at.astimezone(self.timezone).date()
        invalidated = self.forecasts.invalidate_current_for_date(
            self.warnings,
            participant_id,
            local_date,
            reason="observation_committed",
        )
        loop = self._loop
        if loop is None or loop.is_closed() or self._closed:
            logger.error(
                "observation_forecast_refresh_not_running participant_id=%s local_date=%s",
                participant_id,
                local_date,
            )
            return invalidated
        loop.call_soon_threadsafe(self._enqueue, participant_id, local_date)
        return invalidated

    def _enqueue(self, participant_id: uuid.UUID, local_date: date) -> None:
        if self._closed:
            return
        key = (participant_id, local_date)
        self._requested[key] = self._requested.get(key, 0) + 1
        if key in self._tasks:
            return
        task = asyncio.create_task(
            self._recompute_until_current(key),
            name=f"observation-forecast-refresh-{participant_id}-{local_date.isoformat()}",
        )
        self._tasks[key] = task

    async def _recompute_until_current(
        self, key: tuple[uuid.UUID, date]
    ) -> None:
        participant_id, local_date = key
        processed = 0
        try:
            while not self._closed:
                requested = self._requested.get(key, 0)
                if requested <= processed:
                    break
                try:
                    await self.coordinator.ensure_forecast(
                        participant_id,
                        local_date,
                        "observation_committed",
                        refresh_calendar=False,
                        force_followup=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "observation_forecast_refresh_failed participant_id=%s local_date=%s",
                        participant_id,
                        local_date,
                    )
                processed = requested
        finally:
            self._tasks.pop(key, None)
            if self._requested.get(key, 0) <= processed:
                self._requested.pop(key, None)
            elif not self._closed:
                self._enqueue(participant_id, local_date)

    async def wait_idle(self) -> None:
        """Wait for currently tracked refresh work; primarily useful to tests."""

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
