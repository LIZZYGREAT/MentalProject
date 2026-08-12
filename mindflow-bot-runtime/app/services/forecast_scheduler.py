"""Bounded daily forecast preparation and durable warning delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import logging
import uuid
from zoneinfo import ZoneInfo

from app.integrations.feishu.client import FeishuClient, FeishuSendError
from app.repositories import BindingRepository, ParticipantRepository, WarningScheduleRepository
from app.services.forecast_coordinator import ForecastCoordinator


logger = logging.getLogger(__name__)


class ForecastScheduler:
    def __init__(
        self, *, coordinator: ForecastCoordinator, participants: ParticipantRepository,
        warnings: WarningScheduleRepository, bindings: BindingRepository,
        sender: FeishuClient, timezone_name: str, daily_prepare_local_time: str,
        calendar_sync_interval_seconds: int, warning_poll_interval_seconds: int,
        forecast_max_concurrency: int = 1, warning_max_attempts: int = 5,
        warning_retry_base_seconds: int = 60, warning_claim_lease_seconds: int = 120,
    ):
        self.coordinator = coordinator
        self.participants = participants
        self.warnings = warnings
        self.bindings = bindings
        self.sender = sender
        self.timezone = ZoneInfo(timezone_name)
        hour, minute = (int(part) for part in daily_prepare_local_time.split(":"))
        self.daily_time = time(hour, minute)
        self.sync_interval = calendar_sync_interval_seconds
        self.warning_interval = warning_poll_interval_seconds
        self.max_concurrency = max(1, forecast_max_concurrency)
        self.warning_max_attempts = warning_max_attempts
        self.warning_retry_base_seconds = warning_retry_base_seconds
        self.warning_claim_lease_seconds = warning_claim_lease_seconds
        self._stop = asyncio.Event()
        self.started = asyncio.Event()

    async def run_forever(self) -> None:
        self.started.set()
        await asyncio.gather(self._forecast_loop(), self._warning_loop())

    async def close(self) -> None:
        self._stop.set()

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _forecast_loop(self) -> None:
        last_daily = None
        while not self._stop.is_set():
            try:
                now = datetime.now(self.timezone)
                reason = "periodic_poll"
                if now.time() >= self.daily_time and last_daily != now.date():
                    reason = "daily_prepare"
                    last_daily = now.date()
                participant_ids = await asyncio.to_thread(
                    self.participants.active_calendar_ids
                )
                jobs = [
                    (participant_id, now.date() + timedelta(days=offset),
                     reason if offset == 0 else "future_periodic_poll")
                    for participant_id in participant_ids for offset in (0, 1)
                ]
                for start in range(0, len(jobs), self.max_concurrency):
                    batch = jobs[start : start + self.max_concurrency]
                    results = await asyncio.gather(*(
                        self.coordinator.ensure_forecast(pid, target, why)
                        for pid, target, why in batch
                    ), return_exceptions=True)
                    for (pid, target, why), result in zip(batch, results):
                        if isinstance(result, BaseException):
                            logger.error(
                                "forecast_job_failed participant_id=%s local_date=%s reason=%s error_class=%s message=%s",
                                pid, target, why, type(result).__name__, str(result)[:160],
                            )
            except Exception as exc:
                logger.exception(
                    "forecast_scheduler_iteration_failed error_class=%s message=%s",
                    type(exc).__name__, str(exc)[:160],
                )
            await self._wait(self.sync_interval)

    async def _warning_loop(self) -> None:
        while not self._stop.is_set():
            try:
                due = await asyncio.to_thread(
                    self.warnings.pending, datetime.now(timezone.utc)
                )
                for item in due:
                    await self._deliver_warning(item)
                await self._recover_delivery_channels()
            except Exception as exc:
                logger.exception(
                    "warning_scheduler_iteration_failed error_class=%s message=%s",
                    type(exc).__name__, str(exc)[:160],
                )
            await self._wait(self.warning_interval)

    async def _recover_delivery_channels(self) -> None:
        now = datetime.now(timezone.utc)
        blocked = await asyncio.to_thread(self.warnings.delivery_unavailable, now)
        for item in blocked:
            binding = await asyncio.to_thread(
                self.bindings.get_for_participant,
                uuid.UUID(item["participant_id"]),
            )
            await asyncio.to_thread(
                self.warnings.reactivate_delivery, uuid.UUID(item["id"]),
                now=now, available=bool(binding and binding.get("chat_id")),
            )

    async def _deliver_warning(self, item: dict) -> None:
        warning_id = uuid.UUID(item["id"])
        now = datetime.now(timezone.utc)
        claimed = await asyncio.to_thread(
            self.warnings.claim_if_current, warning_id, now=now,
            lease_seconds=self.warning_claim_lease_seconds,
        )
        if not claimed:
            return
        claim_token = claimed["claim_token"]
        expected_forecast_version = claimed["forecast_version"]
        binding = await asyncio.to_thread(
            self.bindings.get_for_participant,
            uuid.UUID(claimed["participant_id"]),
        )
        if not binding or not binding.get("chat_id"):
            await asyncio.to_thread(
                self.warnings.block_delivery, warning_id, now=now,
                claim_token=claim_token,
                expected_forecast_version=expected_forecast_version,
                reason="missing_chat_id",
            )
            return
        if not await asyncio.to_thread(
            self.warnings.validate_claim_current, warning_id,
            claim_token=claim_token,
            expected_forecast_version=expected_forecast_version,
            now=datetime.now(timezone.utc),
        ):
            return
        payload = claimed["payload"]
        text = str(
            payload.get("message")
            or "预测到临近的高压时段，可以提前安排短暂休息。"
        )
        try:
            await asyncio.to_thread(self.sender.send_text, binding["chat_id"], text)
        except FeishuSendError as exc:
            logger.warning(
                "forecast_warning_send_failed warning_id=%s retryable=%s code=%s",
                warning_id, exc.retryable, exc.code,
            )
            await asyncio.to_thread(
                self.warnings.finish_claim, warning_id, sent=False,
                claim_token=claim_token,
                expected_forecast_version=expected_forecast_version,
                now=datetime.now(timezone.utc), retryable=exc.retryable,
                error_code=str(exc.code) if exc.code is not None else None,
                error_class=type(exc).__name__, max_attempts=self.warning_max_attempts,
                retry_base_seconds=self.warning_retry_base_seconds,
            )
            return
        except Exception as exc:
            logger.warning(
                "forecast_warning_send_failed warning_id=%s error_class=%s",
                warning_id, type(exc).__name__,
            )
            await asyncio.to_thread(
                self.warnings.finish_claim, warning_id, sent=False,
                claim_token=claim_token,
                expected_forecast_version=expected_forecast_version,
                now=datetime.now(timezone.utc), retryable=True,
                error_class=type(exc).__name__, max_attempts=self.warning_max_attempts,
                retry_base_seconds=self.warning_retry_base_seconds,
            )
            return
        await asyncio.to_thread(
            self.warnings.finish_claim, warning_id, sent=True,
            claim_token=claim_token,
            expected_forecast_version=expected_forecast_version,
            now=datetime.now(timezone.utc), max_attempts=self.warning_max_attempts,
            retry_base_seconds=self.warning_retry_base_seconds,
        )
