"""Participant-bound reminder tools; schemas never accept identity fields."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.cards import reminder_proposal_confirmation_card
from app.services.presentation_service import ReviewCardPolicy


class ReminderTools:
    def __init__(
        self,
        reminders: Any,
        proactive_policy: Any,
        presentations: Any = None,
        *,
        timezone_name: str,
    ) -> None:
        self.reminders = reminders
        self.proactive_policy = proactive_policy
        self.presentations = presentations
        self.timezone = ZoneInfo(timezone_name)

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "reminder_create",
            "Stage a participant-owned reminder proposal after interpreting the message, exact RFC3339 reminder time, and recurrence. If the time remains ambiguous, ask one clarification question. The reminder is persisted only after the participant confirms the fixed review card.",
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
            self.create,
            effect="proposal_stage",
            authorization_requirement="none",
        )
        registry.register(
            "reminder_list", "List this participant's active reminders and recent delivery failures.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.list, effect="read", authorization_requirement="none",
        )
        registry.register(
            "reminder_cancel", "Stage cancellation of one exact participant-owned reminder. The reminder remains active until the participant confirms the fixed review card.",
            {
                "type": "object",
                "properties": {"reminder_id": {"type": "string", "format": "uuid"}},
                "required": ["reminder_id"], "additionalProperties": False,
            },
            self.cancel,
            effect="proposal_stage",
            authorization_requirement="none",
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
        proposal = self.reminders.stage_create(
            ctx.participant_id, message=args["message"], remind_at=remind_at,
            recurrence_type=args["recurrence_type"],
        )
        local_time = remind_at.astimezone(self.timezone)
        quiet_warning = self.proactive_policy.user_requested_quiet_hours_warning(
            ctx.participant_id, remind_at
        )
        if self.presentations is None:
            raise RuntimeError("reminder proposal presentation is unavailable")
        self.presentations.stage_card(
            ctx.agent_run_id,
            reminder_proposal_confirmation_card(
                proposal, timezone_name=self.timezone.key
            ),
            review_policy=ReviewCardPolicy(
                fallback_text=(
                    "提醒确认卡暂时未能发送，本次提醒尚未保存，请稍后重试。"
                )
            ),
        )
        return {
            "ok": True,
            "reminder_proposal": "pending_confirmation",
            "proposal_id": proposal["id"],
            "confirmation_required": True,
            "persisted": False,
            "quiet_hours_warning": quiet_warning,
            "review": f"请在卡片中核对 {local_time.strftime('%Y-%m-%d %H:%M')} 的提醒。",
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
        proposal = self.reminders.stage_cancel(
            ctx.participant_id, uuid.UUID(args["reminder_id"])
        )
        if proposal is None:
            return {"ok": False, "error": "reminder_not_found"}
        if self.presentations is None:
            raise RuntimeError("reminder proposal presentation is unavailable")
        self.presentations.stage_card(
            ctx.agent_run_id,
            reminder_proposal_confirmation_card(
                proposal, timezone_name=self.timezone.key
            ),
            review_policy=ReviewCardPolicy(
                fallback_text=(
                    "提醒取消确认卡暂时未能发送，原提醒仍然有效，请稍后重试。"
                )
            ),
        )
        return {
            "ok": True,
            "reminder_proposal": "pending_confirmation",
            "proposal_id": proposal["id"],
            "confirmation_required": True,
            "persisted": False,
        }
