"""Deliver neutral supportive follow-ups through the global policy."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import uuid


MESSAGES = {
    "check_in": "想轻轻问一下，你现在还好吗？不方便回复也没关系。",
    "task_transition": "刚才的事情告一段落了吗？可以先给自己一点转换时间。",
    "recovery": "记得给自己留一点恢复时间，不需要马上做更多。",
}


class SupportiveFollowupScheduler:
    def __init__(self, *, candidates, participants, bindings, policy, sender, support_preferences=None, poll_interval_seconds: int = 60) -> None:
        self.candidates = candidates
        self.participants = participants
        self.bindings = bindings
        self.policy = policy
        self.sender = sender
        self.support_preferences = support_preferences
        self.poll_interval_seconds = max(1, poll_interval_seconds)
        self._stop = asyncio.Event()
        self.started = asyncio.Event()

    async def run_once(self, now: datetime | None = None):
        instant = now or datetime.now(timezone.utc)
        instant = (instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant).astimezone(timezone.utc)
        counts = {"sent": 0, "suppressed": 0, "failed": 0}
        for item in await asyncio.to_thread(self.candidates.claim_due, instant):
            participant_id = uuid.UUID(item["participant_id"])
            if self.support_preferences is not None:
                preferences = await asyncio.to_thread(
                    self.support_preferences.get, participant_id
                )
                if not preferences.get("support", {}).get(
                    "allow_supportive_follow_up", True
                ):
                    await asyncio.to_thread(
                        self.candidates.finish, item["id"], item["claim_token"],
                        status="suppressed", now=instant,
                    )
                    counts["suppressed"] += 1
                    continue
            participant = await asyncio.to_thread(self.participants.get, participant_id)
            binding = await asyncio.to_thread(self.bindings.get_for_participant, participant_id)
            if participant is None or participant.status != "active" or not binding or not binding.get("chat_id"):
                await asyncio.to_thread(self.candidates.finish, item["id"], item["claim_token"], status="unavailable", now=instant)
                counts["failed"] += 1
                continue
            decision = await asyncio.to_thread(self.policy.reserve, participant_id, message_kind="supportive_follow_up", dedupe_key=f"followup:{item['id']}", scheduled_at=datetime.fromisoformat(item["due_at"]), now=instant)
            if not decision.allowed:
                await asyncio.to_thread(self.candidates.finish, item["id"], item["claim_token"], status="suppressed", now=instant)
                counts["suppressed"] += 1
                continue
            try:
                await asyncio.to_thread(self.sender.send_text, binding["chat_id"], MESSAGES[item["reason_category"]], message_uuid=str(decision.reservation_id))
            except Exception:
                await asyncio.to_thread(self.policy.release, decision.reservation_id, reason="provider_failed")
                await asyncio.to_thread(self.candidates.finish, item["id"], item["claim_token"], status="failed", now=instant)
                counts["failed"] += 1
                continue
            await asyncio.to_thread(self.policy.mark_sent, decision.reservation_id, now=instant)
            await asyncio.to_thread(self.candidates.finish, item["id"], item["claim_token"], status="sent", now=instant)
            counts["sent"] += 1
        return counts

    async def run_forever(self) -> None:
        self.started.set()
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        self._stop.set()
