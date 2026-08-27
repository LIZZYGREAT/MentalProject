"""Managed background refresh for Forecasts invalidated by Calendar mutations."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import logging
from typing import Iterable, Mapping, TYPE_CHECKING
import uuid

if TYPE_CHECKING:
    from app.repositories_calendar_mutation import (
        CalendarMutationReconciliationRepository,
    )
    from app.services.forecast_coordinator import ForecastCoordinator


logger = logging.getLogger(__name__)


class ForecastMutationRefreshQueue:
    """Dedupe refreshes and serialize dates per participant off request paths."""

    def __init__(
        self,
        coordinator: "ForecastCoordinator",
        *,
        max_concurrency: int = 2,
        invalidation_retry_delays: tuple[float, ...] = (0.05, 0.2),
        reconciliations: "CalendarMutationReconciliationRepository | None" = None,
        recovery_poll_seconds: float = 30.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("forecast mutation refresh concurrency must be positive")
        self.coordinator = coordinator
        self.max_concurrency = max_concurrency
        self.invalidation_retry_delays = invalidation_retry_delays
        self.reconciliations = reconciliations
        self.recovery_poll_seconds = max(0.05, float(recovery_poll_seconds))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._requested: dict[
            uuid.UUID, dict[date, tuple[str, bool]]
        ] = {}
        self._invalidation_requested: dict[
            uuid.UUID, dict[date, str]
        ] = {}
        self._dependency_invalidation_requested: dict[
            uuid.UUID, dict[date, tuple[date, str]]
        ] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._reconciliation_requested: dict[uuid.UUID, set[uuid.UUID]] = {}
        self._active_reconciliation_ids: set[uuid.UUID] = set()
        self._recovery_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("forecast mutation refresh queue is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("forecast mutation refresh queue is already bound")
        self._loop = loop
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        if self.reconciliations is not None and self._recovery_task is None:
            self._recovery_task = asyncio.create_task(
                self._recovery_loop(),
                name="calendar-mutation-reconciliation-recovery",
            )

    def enqueue(
        self,
        participant_id: uuid.UUID,
        dates: Mapping[date, bool],
        *,
        reason: str,
        invalidation_dates: Iterable[date] = (),
        dependency_invalidation_sources: Mapping[date, date] | None = None,
        reconciliation_id: uuid.UUID | str | None = None,
    ) -> bool:
        """Queue target dates mapped to whether Calendar should be refreshed."""

        loop = self._loop
        retry_dates = set(invalidation_dates)
        dependency_retries = dict(dependency_invalidation_sources or {})
        reconciliation_uuid = (
            uuid.UUID(str(reconciliation_id))
            if reconciliation_id is not None
            else None
        )
        if (
            not dates
            and not retry_dates
            and not dependency_retries
            and reconciliation_uuid is None
        ):
            return False
        if loop is None or loop.is_closed() or self._closed:
            logger.error(
                "forecast_mutation_refresh_not_running participant_id=%s",
                participant_id,
            )
            return False
        request = dict(dates)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            self._enqueue(
                participant_id,
                request,
                reason,
                retry_dates,
                dependency_retries,
                reconciliation_uuid,
            )
        else:
            loop.call_soon_threadsafe(
                self._enqueue,
                participant_id,
                request,
                reason,
                retry_dates,
                dependency_retries,
                reconciliation_uuid,
            )
        return True

    def _enqueue(
        self,
        participant_id: uuid.UUID,
        dates: dict[date, bool],
        reason: str,
        invalidation_dates: set[date],
        dependency_invalidation_sources: dict[date, date],
        reconciliation_id: uuid.UUID | None,
    ) -> None:
        if self._closed:
            return
        pending = self._requested.setdefault(participant_id, {})
        for target, refresh_calendar in dates.items():
            previous = pending.get(target)
            if previous is not None and previous[1] and not refresh_calendar:
                # A dependency-only request must not weaken an already queued
                # direct Calendar refresh or replace its audit reason.
                continue
            pending[target] = (reason, bool(refresh_calendar))
        invalidations = self._invalidation_requested.setdefault(
            participant_id, {}
        )
        for target in invalidation_dates:
            invalidations[target] = reason
        dependency_invalidations = self._dependency_invalidation_requested.setdefault(
            participant_id, {}
        )
        for target, source in dependency_invalidation_sources.items():
            dependency_invalidations[target] = (source, reason)
        if reconciliation_id is not None:
            self._reconciliation_requested.setdefault(
                participant_id, set()
            ).add(reconciliation_id)
            self._active_reconciliation_ids.add(reconciliation_id)
        self._start_participant_task(participant_id)

    async def recover_now(self) -> int:
        """Enqueue durable work that is due, including after process restart."""

        if self.reconciliations is None:
            return 0
        rows = await asyncio.to_thread(
            self.reconciliations.due, datetime.now(timezone.utc)
        )
        recovered = 0
        for row in rows:
            reconciliation_id = uuid.UUID(row["id"])
            if reconciliation_id in self._active_reconciliation_ids:
                continue
            targets: dict[date, bool] = {}
            invalidation_dates: set[date] = set()
            dependency_sources: dict[date, date] = {}
            for item in list(row["work"].get("targets") or []):
                target = date.fromisoformat(str(item["local_date"]))
                targets[target] = bool(item.get("refresh_calendar"))
                if row["status"] == "pending" and bool(
                    item.get("requires_invalidation")
                ):
                    invalidation_dates.add(target)
                source = item.get("dependency_source")
                if row["status"] == "pending" and source:
                    dependency_sources[target] = date.fromisoformat(str(source))
            if self.enqueue(
                uuid.UUID(row["participant_id"]),
                targets,
                reason=row["mutation_kind"],
                invalidation_dates=invalidation_dates,
                dependency_invalidation_sources=dependency_sources,
                reconciliation_id=reconciliation_id,
            ):
                recovered += 1
        return recovered

    async def _recovery_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    await self.recover_now()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("calendar_mutation_reconciliation_scan_failed")
                await asyncio.sleep(self.recovery_poll_seconds)
        except asyncio.CancelledError:
            raise

    async def _retry_invalidation(
        self,
        participant_id: uuid.UUID,
        targets: set[date],
        reason: str,
    ) -> bool:
        forecasts = getattr(self.coordinator, "forecasts", None)
        warnings = getattr(self.coordinator, "warnings", None)
        invalidate = getattr(
            forecasts, "invalidate_for_calendar_mutation_dates", None
        )
        if not callable(invalidate) or warnings is None:
            logger.error(
                "forecast_mutation_invalidation_retry_unavailable "
                "participant_id=%s",
                participant_id,
            )
            return False
        attempts = len(self.invalidation_retry_delays) + 1
        for attempt in range(attempts):
            try:
                await asyncio.to_thread(
                    invalidate,
                    warnings,
                    participant_id,
                    targets,
                    reason=reason,
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "forecast_mutation_invalidation_retry_failed "
                    "participant_id=%s attempt=%s",
                    participant_id,
                    attempt + 1,
                )
                if attempt < len(self.invalidation_retry_delays):
                    await asyncio.sleep(self.invalidation_retry_delays[attempt])
        return False

    async def _retry_dependency_invalidation(
        self,
        participant_id: uuid.UUID,
        source: date,
        target: date,
        reason: str,
    ) -> bool:
        dependency_refresh = getattr(
            self.coordinator, "dependency_refresh", None
        )
        invalidate = getattr(
            dependency_refresh, "invalidate_dependent_now", None
        )
        args = (participant_id, source)
        if not callable(invalidate):
            invalidate = getattr(self.coordinator, "mark_dependency_dirty", None)
            args = (participant_id, target)
        if not callable(invalidate):
            logger.error(
                "forecast_mutation_dependency_retry_unavailable "
                "participant_id=%s local_date=%s",
                participant_id,
                target,
            )
            return False
        attempts = len(self.invalidation_retry_delays) + 1
        for attempt in range(attempts):
            try:
                await asyncio.to_thread(
                    invalidate,
                    *args,
                    reason=reason,
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "forecast_mutation_dependency_retry_failed "
                    "participant_id=%s local_date=%s attempt=%s",
                    participant_id,
                    target,
                    attempt + 1,
                )
                if attempt < len(self.invalidation_retry_delays):
                    await asyncio.sleep(self.invalidation_retry_delays[attempt])
        return False

    def _start_participant_task(self, participant_id: uuid.UUID) -> None:
        if self._closed or participant_id in self._tasks:
            return

        async def run() -> None:
            semaphore = self._semaphore
            if semaphore is None:
                raise RuntimeError("forecast mutation refresh queue is not started")
            active_in_run: set[uuid.UUID] = set()
            try:
                async with semaphore:
                    failed_targets: set[date] = set()
                    while not self._closed:
                        batch = self._requested.pop(participant_id, {})
                        invalidation_batch = self._invalidation_requested.pop(
                            participant_id, {}
                        )
                        dependency_invalidation_batch = (
                            self._dependency_invalidation_requested.pop(
                                participant_id, {}
                            )
                        )
                        reconciliation_ids = self._reconciliation_requested.pop(
                            participant_id, set()
                        )
                        active_in_run.update(reconciliation_ids)
                        if (
                            not batch
                            and not invalidation_batch
                            and not dependency_invalidation_batch
                            and not reconciliation_ids
                        ):
                            break
                        grouped_invalidations: dict[str, set[date]] = {}
                        for target, reason in invalidation_batch.items():
                            grouped_invalidations.setdefault(reason, set()).add(
                                target
                            )
                        for invalidation_reason, targets in grouped_invalidations.items():
                            if await self._retry_invalidation(
                                participant_id, targets, invalidation_reason
                            ):
                                failed_targets.difference_update(targets)
                            else:
                                failed_targets.update(targets)
                        for target, (
                            source,
                            invalidation_reason,
                        ) in dependency_invalidation_batch.items():
                            if await self._retry_dependency_invalidation(
                                participant_id,
                                source,
                                target,
                                invalidation_reason,
                            ):
                                failed_targets.discard(target)
                            else:
                                failed_targets.add(target)
                        invalidation_targets = (
                            set(invalidation_batch)
                            | set(dependency_invalidation_batch)
                        )
                        invalidation_failed = invalidation_targets & failed_targets
                        if (
                            reconciliation_ids
                            and self.reconciliations is not None
                            and not invalidation_failed
                        ):
                            for reconciliation_id in reconciliation_ids:
                                await asyncio.to_thread(
                                    self.reconciliations.mark_fenced,
                                    reconciliation_id,
                                )
                        for target in sorted(batch):
                            reason, refresh_calendar = batch[target]
                            if target in failed_targets:
                                logger.error(
                                    "forecast_mutation_refresh_skipped_unfenced "
                                    "participant_id=%s local_date=%s",
                                    participant_id,
                                    target,
                                )
                                continue
                            if (
                                not refresh_calendar
                                and target > date.min
                                and date.fromordinal(target.toordinal() - 1)
                                in failed_targets
                            ):
                                logger.error(
                                    "forecast_mutation_dependency_source_failed "
                                    "participant_id=%s local_date=%s",
                                    participant_id,
                                    target,
                                )
                                continue
                            try:
                                await self.coordinator.ensure_forecast(
                                    participant_id,
                                    target,
                                    reason,
                                    refresh_calendar=refresh_calendar,
                                    force_followup=True,
                                )
                                failed_targets.discard(target)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                failed_targets.add(target)
                                logger.exception(
                                    "forecast_mutation_refresh_failed "
                                    "participant_id=%s local_date=%s",
                                    participant_id,
                                    target,
                                )
                        if reconciliation_ids and self.reconciliations is not None:
                            relevant_targets = (
                                set(batch)
                                | set(invalidation_batch)
                                | set(dependency_invalidation_batch)
                            )
                            unresolved = relevant_targets & failed_targets
                            for reconciliation_id in reconciliation_ids:
                                if unresolved:
                                    await asyncio.to_thread(
                                        self.reconciliations.mark_retry,
                                        reconciliation_id,
                                        error_class="CalendarMutationReconciliationFailed",
                                    )
                                else:
                                    await asyncio.to_thread(
                                        self.reconciliations.mark_resolved,
                                        reconciliation_id,
                                    )
                                self._active_reconciliation_ids.discard(
                                    reconciliation_id
                                )
                                active_in_run.discard(reconciliation_id)
            except asyncio.CancelledError:
                raise
            finally:
                self._tasks.pop(participant_id, None)
                abandoned = self._reconciliation_requested.pop(
                    participant_id, set()
                )
                for reconciliation_id in abandoned:
                    self._active_reconciliation_ids.discard(reconciliation_id)
                for reconciliation_id in active_in_run:
                    self._active_reconciliation_ids.discard(reconciliation_id)
                if (
                    (
                        self._requested.get(participant_id)
                        or self._invalidation_requested.get(participant_id)
                        or self._dependency_invalidation_requested.get(
                            participant_id
                        )
                    )
                    and not self._closed
                ):
                    self._start_participant_task(participant_id)

        self._tasks[participant_id] = asyncio.create_task(
            run(),
            name=f"forecast-mutation-refresh-{participant_id}",
        )

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(
                *tuple(self._tasks.values()), return_exceptions=True
            )

    async def close(self) -> None:
        self._closed = True
        recovery_task = self._recovery_task
        if recovery_task is not None:
            recovery_task.cancel()
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if recovery_task is not None:
            await asyncio.gather(recovery_task, return_exceptions=True)
        self._recovery_task = None
        self._tasks.clear()
        self._requested.clear()
        self._invalidation_requested.clear()
        self._dependency_invalidation_requested.clear()
        self._reconciliation_requested.clear()
        self._active_reconciliation_ids.clear()
