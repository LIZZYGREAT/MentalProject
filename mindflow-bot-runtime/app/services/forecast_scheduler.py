"""Async daily preparation, periodic refresh, and durable warning recovery."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import logging
import uuid
from zoneinfo import ZoneInfo

from app.integrations.feishu.client import FeishuClient
from app.repositories import BindingRepository, ParticipantRepository, WarningScheduleRepository
from app.services.forecast_coordinator import ForecastCoordinator


logger = logging.getLogger(__name__)


class ForecastScheduler:
    def __init__(
        self, *, coordinator: ForecastCoordinator, participants: ParticipantRepository,
        warnings: WarningScheduleRepository, bindings: BindingRepository,
        sender: FeishuClient, timezone_name: str, daily_prepare_local_time: str,
        calendar_sync_interval_seconds: int, warning_poll_interval_seconds: int,
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
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        await asyncio.gather(self._forecast_loop(), self._warning_loop())

    async def close(self) -> None:
        self._stop.set()

    async def _forecast_loop(self) -> None:
        last_daily = None
        while not self._stop.is_set():
            now = datetime.now(self.timezone)
            reason = "periodic_poll"
            if now.time() >= self.daily_time and last_daily != now.date():
                reason = "daily_prepare"
                last_daily = now.date()
            participant_ids = await asyncio.to_thread(self.participants.active_ids)
            await asyncio.gather(*(
                self.coordinator.ensure_forecast(
                    participant_id, now.date() + timedelta(days=offset),
                    reason if offset == 0 else "future_periodic_poll",
                )
                for participant_id in participant_ids
                for offset in (0, 1)
            ), return_exceptions=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.sync_interval)
            except asyncio.TimeoutError:
                pass

    async def _warning_loop(self) -> None:
        # Pending rows are the recovery source after every process restart.
        while not self._stop.is_set():
            due = await asyncio.to_thread(self.warnings.pending, datetime.now(timezone.utc))
            for item in due:
                warning_id = uuid.UUID(item["id"])
                claimed = await asyncio.to_thread(self.warnings.claim_if_current, warning_id)
                if not claimed:
                    continue
                binding = await asyncio.to_thread(
                    self.bindings.get_for_participant, uuid.UUID(claimed["participant_id"])
                )
                sent = False
                try:
                    if binding and binding.get("chat_id"):
                        payload = claimed["payload"]
                        text = str(payload.get("message") or "预测到临近的高压时段，可以提前安排短暂休息。")
                        await asyncio.to_thread(self.sender.send_text, binding["chat_id"], text)
                        sent = True
                except Exception:
                    logger.warning("forecast_warning_send_failed", exc_info=True)
                await asyncio.to_thread(
                    self.warnings.finish_claim, warning_id,
                    sent=sent, now=datetime.now(timezone.utc),
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.warning_interval)
            except asyncio.TimeoutError:
                pass
