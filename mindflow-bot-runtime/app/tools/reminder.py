"""Participant-bound reminder tools; schemas never accept identity fields."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo
import uuid
from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry


_CLOCK = re.compile(
    r"(?:(?<!\d)(?:[01]?\d|2[0-3])[:：][0-5]\d(?!\d)|"
    r"(?:上午|下午|晚上|中午|凌晨)?\s*\d{1,2}\s*点(?:半|\s*\d{1,2}\s*分)?|"
    r"\b\d{1,2}(?::[0-5]\d)?\s*(?:am|pm)\b)",
    re.I,
)
_DATE = re.compile(
    r"(?:今天|明天|后天|大后天|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}[日号]?|"
    r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b)",
    re.I,
)
_DURATION = re.compile(
    r"(?:\d+\s*(?:分钟|小时|天)\s*后|半\s*小时\s*后|"
    r"\d+\s*(?:minutes?|hours?|days?)\s*later)",
    re.I,
)
_REMINDER_INTENT = re.compile(r"(?:提醒|叫我|通知我|remind\s+me)", re.I)
_CHINESE_TIME_NUMBER = re.compile(
    r"(?<![周期])([零〇一二两三四五六七八九十]{1,3})"
    r"(?=\s*(?:点|分钟|小时|天))"
)
_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _chinese_number(value: str) -> int:
    if "十" not in value:
        digits = "".join(str(_CHINESE_DIGITS[char]) for char in value)
        return int(digits)
    left, right = value.split("十", 1)
    tens = _CHINESE_DIGITS[left] if left else 1
    ones = _CHINESE_DIGITS[right] if right else 0
    return tens * 10 + ones


def _normalize_time_words(value: str) -> str:
    return _CHINESE_TIME_NUMBER.sub(
        lambda match: str(_chinese_number(match.group(1))), str(value)
    )


def _clock_only_clarification(value: str) -> bool:
    candidate = _normalize_time_words(value).strip()
    candidate = re.sub(
        r"^(?:就|那就|定在|改成|时间(?:是|为)?)\s*", "", candidate
    )
    candidate = re.sub(
        r"\s*(?:吧|可以|就好|谢谢)?[。！!]?$", "", candidate
    ).strip()
    return bool(_CLOCK.fullmatch(candidate))


def has_exact_time_grounding(ctx: AgentContext) -> bool:
    current = _normalize_time_words(ctx.user_request_text)
    if _DURATION.search(current) or (
        _DATE.search(current) and _CLOCK.search(current)
    ):
        return True
    if not _clock_only_clarification(current):
        return False

    prior_user_turns = [
        str(item.text)
        for item in ctx.authorization_semantic_context
        if getattr(item, "role", None) == "user" and str(item.text).strip()
    ]
    # Production semantic context normally includes the current turn last.
    # Never use it as its own clarification predecessor.
    if prior_user_turns and prior_user_turns[-1].strip() == str(
        ctx.user_request_text
    ).strip():
        prior_user_turns.pop()
    if not prior_user_turns:
        return False
    previous = _normalize_time_words(prior_user_turns[-1])
    return bool(_REMINDER_INTENT.search(previous) and _DATE.search(previous))


class ReminderTools:
    def __init__(self, reminders: Any, proactive_policy: Any, *, timezone_name: str) -> None:
        self.reminders = reminders
        self.proactive_policy = proactive_policy
        self.timezone = ZoneInfo(timezone_name)

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "reminder_create",
            "Create a participant-owned reminder only after message and exact date-time are explicit. If time is vague, ask one clarification question instead of calling this tool.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "minLength": 1, "maxLength": 500},
                    "remind_at": {"type": "string", "format": "date-time"},
                    "recurrence_type": {"type": "string", "enum": ["none", "daily", "weekly"]},
                },
                "required": ["message", "remind_at", "recurrence_type"],
                "additionalProperties": False,
            },
            self.create, effect="internal_write", authorization_requirement="direct_request",
        )
        registry.register(
            "reminder_list", "List this participant's active reminders and recent delivery failures.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.list, effect="read", authorization_requirement="none",
        )
        registry.register(
            "reminder_cancel", "Cancel one exact participant-owned reminder after a direct request.",
            {
                "type": "object",
                "properties": {"reminder_id": {"type": "string", "format": "uuid"}},
                "required": ["reminder_id"], "additionalProperties": False,
            },
            self.cancel, effect="internal_write", authorization_requirement="direct_request",
        )

    def create(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        if not has_exact_time_grounding(ctx):
            return {
                "ok": False,
                "error": "reminder_time_needs_clarification",
                "detail": "The user's own words do not contain an exact reminder time.",
            }
        raw = str(args["remind_at"])
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        remind_at = datetime.fromisoformat(raw)
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=self.timezone)
        row = self.reminders.create(
            ctx.participant_id, message=args["message"], remind_at=remind_at,
            recurrence_type=args["recurrence_type"],
        )
        local_time = remind_at.astimezone(self.timezone)
        quiet_warning = self.proactive_policy.user_requested_quiet_hours_warning(
            ctx.participant_id, remind_at
        )
        return {
            "ok": True, "reminder": {key: value for key, value in row.items() if key not in {"participant_id", "claim_token"}},
            "quiet_hours_warning": quiet_warning,
            "confirmation": f"已设置在 {local_time.strftime('%Y-%m-%d %H:%M')} 提醒。",
        }

    def list(self, ctx: AgentContext, _args: dict[str, Any]) -> dict[str, Any]:
        reminders = []
        for row in self.reminders.list_for_user(ctx.participant_id):
            item = {
                key: value
                for key, value in row.items()
                if key not in {"participant_id", "claim_token"}
            }
            if row.get("status") == "delivery_failed":
                item["delivery_notice"] = "这条提醒未能成功送达"
            reminders.append(item)
        return {"ok": True, "reminders": reminders}

    def cancel(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        cancelled = self.reminders.cancel(ctx.participant_id, uuid.UUID(args["reminder_id"]))
        return {"ok": cancelled, "error": None if cancelled else "reminder_not_found"}
