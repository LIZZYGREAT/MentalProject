"""Participant-bound reminder tools; schemas never accept identity fields."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import AuthorizationContextResolutionError, ToolRegistry


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
            authorization_context_resolver=self.authorization_context,
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
        raw = str(args.get("remind_at") or "").strip()
        if "T" not in raw:
            return {
                "ok": False,
                "error": "invalid_reminder_datetime",
                "detail": "remind_at must be an RFC3339 datetime.",
            }
        try:
            remind_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return {
                "ok": False,
                "error": "invalid_reminder_datetime",
                "detail": "remind_at must be an RFC3339 datetime.",
            }
        if remind_at.tzinfo is None or remind_at.utcoffset() is None:
            return {
                "ok": False,
                "error": "reminder_timezone_required",
                "detail": "remind_at must include a timezone offset.",
            }
        if remind_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            return {
                "ok": False,
                "error": "reminder_time_in_past",
                "detail": "remind_at must be in the future.",
            }
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

    def authorization_context(
        self, ctx: AgentContext, _args: dict[str, Any]
    ) -> dict[str, Any]:
        reference = ctx.received_at_utc
        if reference is None:
            raise AuthorizationContextResolutionError(
                "reminder_reference_time_unavailable"
            )
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        return {
            "reminder_time_context": {
                "reference_time_utc": reference.astimezone(timezone.utc).isoformat(),
                "timezone": self.timezone.key,
            },
            "reminder_semantic_contract": {
                "exact_time_required": True,
                "ambiguous_time_requires_clarification": True,
            },
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
