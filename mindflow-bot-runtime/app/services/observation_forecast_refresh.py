"""Forecast invalidation and managed recompute after an Observation commit."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import logging
import uuid
from zoneinfo import ZoneInfo

from app.repositories import ForecastSnapshotRepository, WarningScheduleRepository
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.forecast_dependency_refresh import ForecastDependencyRefreshService


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
        dependency_refresh: ForecastDependencyRefreshService | None = None,
    ) -> None:
        self.forecasts = forecasts
        self.warnings = warnings
        self.coordinator = coordinator
        self.timezone = ZoneInfo(timezone_name)
        self.dependency_refresh = dependency_refresh
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
        if self.dependency_refresh is not None:
            dependent_invalidated = self.dependency_refresh.invalidate_dependent_now(
                participant_id,
                local_date,
                reason="previous_day_observation_input_changed",
            )
            invalidated = {
                key: invalidated.get(key, 0) + dependent_invalidated.get(key, 0)
                for key in {**invalidated, **dependent_invalidated}
            }
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
                    if self.dependency_refresh is not None:
                        await self.dependency_refresh.refresh_dependent_after_source(
                            participant_id,
                            local_date,
                            reason="previous_day_observation_terminal_changed",
                        )
                    else:
                        dependent_date = local_date + timedelta(days=1)
                        await asyncio.to_thread(
                            self.coordinator.mark_dependency_dirty,
                            participant_id,
                            dependent_date,
                            reason="previous_day_observation_terminal_changed",
                        )
                        await self.coordinator.ensure_forecast(
                            participant_id,
                            dependent_date,
                            "previous_day_observation_terminal_changed",
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
