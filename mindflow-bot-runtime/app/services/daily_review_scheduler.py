"""Durable, restart-safe 22:00 Daily Review card delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import logging
import uuid
from zoneinfo import ZoneInfo

from app.integrations.feishu.cards import daily_review_card
from app.repositories import BindingRepository, ParticipantRepository
from app.repositories_daily_review import DailyReviewScheduleRepository


logger = logging.getLogger(__name__)


class DailyReviewScheduler:
    def __init__(
        self, *, schedules: DailyReviewScheduleRepository,
        participants: ParticipantRepository, bindings: BindingRepository,
        sender: object, timezone_name: str = "Asia/Shanghai",
        local_time: str = "22:00", poll_interval_seconds: int = 60,
        retry_base_seconds: int = 60, max_attempts: int = 5,
        claim_lease_seconds: int = 120,
        validity_minutes: int = 1440, catch_up_minutes: int = 120,
    ):
        self.schedules = schedules
        self.participants = participants
        self.bindings = bindings
        self.sender = sender
        self.timezone = ZoneInfo(timezone_name)
        hour, minute = (int(part) for part in local_time.split(":"))
        self.local_time = time(hour, minute)
        self.poll_interval_seconds = poll_interval_seconds
        self.retry_base_seconds = retry_base_seconds
        self.max_attempts = max_attempts
        self.claim_lease_seconds = claim_lease_seconds
        self.validity_minutes = max(1, int(validity_minutes))
        self.catch_up_minutes = max(0, int(catch_up_minutes))
        if self.catch_up_minutes > self.validity_minutes:
            raise ValueError("catch_up_minutes must not exceed validity_minutes")
        self._stop = asyncio.Event()
        self.started = asyncio.Event()

    async def run_once(self, now: datetime | None = None) -> dict[str, int]:
        utc_now = now or datetime.now(timezone.utc)
        if utc_now.tzinfo is None:
            utc_now = utc_now.replace(tzinfo=timezone.utc)
        local_now = utc_now.astimezone(self.timezone)
        counts = {"ensured": 0, "sent": 0, "unavailable": 0, "failed": 0}
        schedule_dates = []
        if local_now.time() >= self.local_time:
            schedule_dates.append(local_now.date())
        elif self.catch_up_minutes > 0:
            catch_up_deadline = datetime.combine(
                local_now.date(), time.min, self.timezone
            ) + timedelta(minutes=self.catch_up_minutes)
            if local_now < catch_up_deadline:
                schedule_dates.append(local_now.date() - timedelta(days=1))

        participant_ids = await asyncio.to_thread(self.participants.active_ids)
        for schedule_date in schedule_dates:
            scheduled_local = datetime.combine(
                schedule_date, self.local_time, self.timezone
            )
            valid_until = scheduled_local + timedelta(
                minutes=self.validity_minutes
            )
            if local_now >= valid_until:
                continue
            for participant_id in participant_ids:
                await asyncio.to_thread(
                    self.schedules.ensure, participant_id, schedule_date,
                    scheduled_local.astimezone(timezone.utc),
                    valid_until=valid_until.astimezone(timezone.utc),
                )
                counts["ensured"] += 1
        for participant_id in participant_ids:
            binding = await asyncio.to_thread(
                self.bindings.get_for_participant, participant_id
            )
            if binding and binding.get("chat_id"):
                await asyncio.to_thread(
                    self.schedules.reactivate_available, participant_id, utc_now
                )
        claimed = await asyncio.to_thread(
            self.schedules.claim_due, utc_now, self.claim_lease_seconds
        )
        for item in claimed:
            binding = await asyncio.to_thread(
                self.bindings.get_for_participant, uuid.UUID(item["participant_id"])
            )
            if not binding or not binding.get("chat_id"):
                await asyncio.to_thread(
                    self.schedules.mark_unavailable, item["id"], item["claim_token"],
                    now=utc_now,
                )
                counts["unavailable"] += 1
                continue
            card = daily_review_card(
                schedule_id=item["id"], local_date=item["local_date"],
                card_version=item["card_version"],
            )
            try:
                message_id = await asyncio.to_thread(
                    self.sender.send_card, binding["chat_id"], card,
                    message_uuid=item["id"],
                )
            except Exception as exc:
                logger.warning(
                    "daily_review_send_failed schedule_id=%s error_class=%s",
                    item["id"], type(exc).__name__,
                )
                await asyncio.to_thread(
                    self.schedules.mark_failed, item["id"], item["claim_token"],
                    now=utc_now, error=exc, max_attempts=self.max_attempts,
                    retry_base_seconds=self.retry_base_seconds,
                )
                counts["failed"] += 1
                continue
            await asyncio.to_thread(
                self.schedules.mark_sent, item["id"], item["claim_token"],
                now=utc_now, provider_message_id=message_id,
            )
            counts["sent"] += 1
        return counts

    async def run_forever(self) -> None:
        self.started.set()
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("daily_review_scheduler_iteration_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        self._stop.set()
