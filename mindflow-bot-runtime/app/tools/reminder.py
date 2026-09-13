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
_DURATION = re.compile(r"\d+\s*(?:分钟|小时|天|minutes?|hours?|days?)\s*(?:后|later)", re.I)


def has_exact_time_grounding(ctx: AgentContext) -> bool:
    texts = [ctx.user_request_text]
    texts.extend(
        item.text
        for item in ctx.authorization_semantic_context
        if getattr(item, "role", None) == "user"
    )
    combined = " ".join(str(value) for value in texts if str(value).strip())
    return bool(_DURATION.search(combined) or (_DATE.search(combined) and _CLOCK.search(combined)))


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
            "reminder_list", "List this participant's active reminders.",
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
        rows = self.reminders.list_active(ctx.participant_id)
        return {"ok": True, "reminders": [{key: value for key, value in row.items() if key not in {"participant_id", "claim_token"}} for row in rows]}

    def cancel(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        cancelled = self.reminders.cancel(ctx.participant_id, uuid.UUID(args["reminder_id"]))
        return {"ok": cancelled, "error": None if cancelled else "reminder_not_found"}
