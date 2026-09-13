"""Opt-in deterministic morning brief generation and delivery."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Any, Callable
import uuid
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)
MORNING_BRIEF_HINT = "给今天留一点余量，按自己的节奏来就好。"
FORBIDDEN_BRIEF_TERMS = frozenset({"压力预测", "压力等级", "高压峰值", "AUC", "风险窗口", "模型解释"})


def render_morning_brief(
    local_date: str,
    events: list[dict[str, Any]],
    reminders: list[dict[str, Any]],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    """Render factual agenda/reminders plus one fixed non-predictive hint."""
    lines = [f"早上好，今天是 {local_date}。", "", "今日日程："]
    if events:
        for event in events[:20]:
            summary = str(event.get("summary") or event.get("title") or "未命名安排")[:120]
            start = str(event.get("start_time") or event.get("start") or "")
            display_time = "时间待定"
            if start:
                try:
                    parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
                    display_time = parsed.astimezone(
                        ZoneInfo(timezone_name)
                    ).strftime("%H:%M")
                except ValueError:
                    pass
            lines.append(f"- {display_time} {summary}")
    else:
        lines.append("- 暂无日程")
    lines.extend(["", "提醒事项："])
    if reminders:
        lines.extend(f"- {str(item.get('message') or '')[:200]}" for item in reminders[:20])
    else:
        lines.append("- 暂无提醒事项")
    lines.extend(["", MORNING_BRIEF_HINT])
    return "\n".join(lines)


class MorningBriefScheduler:
    def __init__(self, *, schedules: Any, participants: Any, bindings: Any, care_preferences: Any, proactive_policy: Any, calendar: Any, sender: Any, reminder_source: Any = None, timezone_name: str = "Asia/Shanghai", poll_interval_seconds: int = 60, catch_up_minutes: int = 120, validity_minutes: int = 120, claim_lease_seconds: int = 120, retry_base_seconds: int = 60, max_attempts: int = 5, clock: Callable[[], datetime] | None = None) -> None:
        self.schedules = schedules
        self.participants = participants
        self.bindings = bindings
        self.care_preferences = care_preferences
        self.proactive_policy = proactive_policy
        self.calendar = calendar
        self.sender = sender
        self.reminder_source = reminder_source
        self.timezone = ZoneInfo(timezone_name)
        self.poll_interval_seconds = max(1, poll_interval_seconds)
        self.catch_up_minutes = max(0, catch_up_minutes)
        self.validity_minutes = max(1, validity_minutes)
        self.claim_lease_seconds = max(1, claim_lease_seconds)
        self.retry_base_seconds = max(1, retry_base_seconds)
        self.max_attempts = max(1, max_attempts)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = asyncio.Event()
        self.started = asyncio.Event()

    async def run_once(self, now: datetime | None = None) -> dict[str, int]:
        instant = now or self.clock()
        instant = (instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant).astimezone(timezone.utc)
        local_now = instant.astimezone(self.timezone)
        counts = {"ensured": 0, "sent": 0, "failed": 0, "suppressed": 0}
        participant_ids = await asyncio.to_thread(self.participants.active_ids)
        for participant_id in participant_ids:
            preferences = await asyncio.to_thread(self.care_preferences.get, participant_id)
            if not preferences.get("morning_brief_enabled", False):
                continue
            hour, minute = map(int, preferences["morning_brief_local_time"].split(":"))
            scheduled_local = datetime.combine(local_now.date(), time(hour, minute), self.timezone)
            if not scheduled_local <= local_now < scheduled_local + timedelta(minutes=self.catch_up_minutes):
                continue
            await asyncio.to_thread(self.schedules.ensure, participant_id, local_now.date(), scheduled_local.astimezone(timezone.utc), valid_until=(scheduled_local + timedelta(minutes=self.validity_minutes)).astimezone(timezone.utc))
            counts["ensured"] += 1
        claimed = await asyncio.to_thread(self.schedules.claim_due, instant, self.claim_lease_seconds)
        for item in claimed:
            participant_id = uuid.UUID(item["participant_id"])
            preferences = await asyncio.to_thread(self.care_preferences.get, participant_id)
            if not preferences.get("morning_brief_enabled", False):
                await asyncio.to_thread(self.schedules.finish, item["id"], item["claim_token"], status="cancelled", now=instant, error_code="feature_disabled")
                counts["suppressed"] += 1
                continue
            binding = await asyncio.to_thread(self.bindings.get_for_participant, participant_id)
            if not binding or not binding.get("chat_id"):
                await asyncio.to_thread(self.schedules.finish, item["id"], item["claim_token"], status="unavailable", now=instant, error_code="binding_unavailable")
                counts["failed"] += 1
                continue
            decision = await asyncio.to_thread(self.proactive_policy.reserve, participant_id, message_kind="morning_brief", dedupe_key=f"morning:{item['local_date']}", scheduled_at=datetime.fromisoformat(item["scheduled_at"]), now=instant)
            if not decision.allowed:
                await asyncio.to_thread(self.schedules.finish, item["id"], item["claim_token"], status="suppressed", now=instant, error_code=decision.reason)
                counts["suppressed"] += 1
                continue
            item_date = date.fromisoformat(item["local_date"])
            day_start = datetime.combine(item_date, time.min, self.timezone)
            try:
                events = await self.calendar.get_events(participant_id, day_start, day_start + timedelta(days=1))
            except Exception:
                logger.info("morning_brief_calendar_unavailable")
                events = []
            reminders = await asyncio.to_thread(
                self.reminder_source.for_local_date, participant_id, item_date
            ) if self.reminder_source is not None else []
            message = render_morning_brief(
                item["local_date"], events, reminders,
                timezone_name=self.timezone.key,
            )
            try:
                provider_id = await asyncio.to_thread(self.sender.send_text, binding["chat_id"], message, message_uuid=item["id"])
            except Exception as exc:
                await asyncio.to_thread(self.proactive_policy.release, decision.reservation_id, reason="provider_failed")
                terminal = item["attempt_count"] >= self.max_attempts
                await asyncio.to_thread(self.schedules.finish, item["id"], item["claim_token"], status="failed" if terminal else "pending", now=instant, error_code=type(exc).__name__, retry_after_seconds=None if terminal else self.retry_base_seconds)
                counts["failed"] += 1
                continue
            await asyncio.to_thread(self.proactive_policy.mark_sent, decision.reservation_id, now=instant)
            await asyncio.to_thread(self.schedules.finish, item["id"], item["claim_token"], status="sent", now=instant, provider_message_id=provider_id)
            counts["sent"] += 1
        return counts

    async def run_forever(self) -> None:
        self.started.set()
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("morning_brief_scheduler_iteration_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        self._stop.set()
