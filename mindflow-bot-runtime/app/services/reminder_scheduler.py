"""Restart-safe delivery for participant-requested reminders."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import uuid


logger = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(self, *, reminders, participants, bindings, proactive_policy, sender, poll_interval_seconds: int = 30, claim_lease_seconds: int = 120) -> None:
        self.reminders = reminders
        self.participants = participants
        self.bindings = bindings
        self.proactive_policy = proactive_policy
        self.sender = sender
        self.poll_interval_seconds = max(1, poll_interval_seconds)
        self.claim_lease_seconds = max(1, claim_lease_seconds)
        self._stop = asyncio.Event()
        self.started = asyncio.Event()

    async def run_once(self, now: datetime | None = None) -> dict[str, int]:
        instant = now or datetime.now(timezone.utc)
        instant = (instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant).astimezone(timezone.utc)
        counts = {"sent": 0, "failed": 0}
        claimed = await asyncio.to_thread(self.reminders.claim_due, instant, self.claim_lease_seconds)
        for item in claimed:
            participant_id = uuid.UUID(item["participant_id"])
            participant = await asyncio.to_thread(self.participants.get, participant_id)
            binding = await asyncio.to_thread(self.bindings.get_for_participant, participant_id)
            if participant is None or participant.status != "active" or not binding or not binding.get("chat_id"):
                await asyncio.to_thread(self.reminders.release, item["id"], item["claim_token"])
                counts["failed"] += 1
                continue
            decision = await asyncio.to_thread(
                self.proactive_policy.reserve, participant_id,
                message_kind="reminder", dedupe_key=f"reminder:{item['id']}:{item['fired_count']}",
                scheduled_at=datetime.fromisoformat(item["next_fire_at"]), now=instant,
            )
            if not decision.allowed:
                await asyncio.to_thread(self.reminders.release, item["id"], item["claim_token"])
                counts["failed"] += 1
                continue
            try:
                self.sender.send_text(
                    binding["chat_id"], f"提醒：{item['message']}",
                    message_uuid=str(decision.reservation_id),
                )
            except Exception:
                logger.warning("reminder_send_failed", exc_info=True)
                await asyncio.to_thread(self.proactive_policy.release, decision.reservation_id, reason="provider_failed")
                await asyncio.to_thread(self.reminders.release, item["id"], item["claim_token"])
                counts["failed"] += 1
                continue
            await asyncio.to_thread(self.proactive_policy.mark_sent, decision.reservation_id, now=instant)
            await asyncio.to_thread(self.reminders.mark_fired, item["id"], item["claim_token"], fired_at=instant)
            counts["sent"] += 1
        return counts

    async def run_forever(self) -> None:
        self.started.set()
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("reminder_scheduler_iteration_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        self._stop.set()
