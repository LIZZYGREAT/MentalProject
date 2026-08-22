"""Fixed, participant-bound handlers for Feishu card actions.

Card callbacks never enter the language model.  The action allowlist and field
validation here are the authority for any state change triggered by a card.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
import hashlib
import json
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from app.integrations.feishu.cards import daily_checkin_card, today_calendar_card
from app.repositories import ObservationRepository


def _boolean(value: Any, field: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{field} must be true or false")


def _score(value: Any, field: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not 0 <= score <= 10:
        raise ValueError(f"{field} must be between 0 and 10")
    return score


class CardActionService:
    def __init__(
        self,
        observations: ObservationRepository,
        calendar: Any = None,
        *,
        timezone_name: str = "Asia/Shanghai",
    ):
        self.observations = observations
        self.calendar = calendar
        self.timezone = ZoneInfo(timezone_name)

    @staticmethod
    def _fallback_event_id(
        message_id: str,
        action_value: dict[str, Any],
        form_value: dict[str, Any],
    ) -> str:
        serialized = json.dumps(
            {
                "message_id": str(message_id),
                "action": action_value,
                "form_value": form_value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return "card:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def handle(
        self,
        participant_id: uuid.UUID,
        *,
        message_id: str,
        callback_event_id: str | None = None,
        action_value: dict[str, Any] | None,
        form_value: dict[str, Any] | None,
    ) -> dict[str, Any]:
        action = dict(action_value or {})
        action_name = str(action.get("mindflow_action") or "")
        if action_name == "request_checkin":
            return {
                "ok": True,
                "reply_text": "请填写此刻状态。",
                "card": daily_checkin_card(),
            }
        if action_name == "view_today_calendar":
            if self.calendar is None:
                raise RuntimeError("calendar service is unavailable")
            import asyncio

            today = datetime.now(self.timezone).date()
            start = datetime.combine(today, time.min, self.timezone)
            events = asyncio.run(
                self.calendar.get_events(participant_id, start, start + timedelta(days=1))
            )
            return {
                "ok": True,
                "reply_text": "已加载今日日程。",
                "card": today_calendar_card(events, local_date=today.isoformat()),
            }
        if (
            action_name != "submit_checkin"
            or str(action.get("version") or "") != "1"
        ):
            return {"ok": False, "error": "unsupported_card_action"}
        values = dict(form_value or {})
        stress = _score(values.get("stress"), "stress")
        energy = _score(values.get("energy"), "energy")
        activity = str(values.get("activity") or "").strip()
        if not 1 <= len(activity) <= 120:
            raise ValueError("activity must be 1-120 characters")
        stress_event = _boolean(
            values.get("stress_event_since_last"), "stress_event_since_last"
        )
        ongoing = _boolean(values.get("event_ongoing"), "event_ongoing")
        event_id = str(callback_event_id or "").strip() or self._fallback_event_id(
            message_id, action, values
        )
        observation_id = self.observations.add(
            participant_id,
            "checkin",
            {
                "stress_0_10": stress,
                "energy_0_10": energy,
                "activity": activity,
                "stress_event_since_last": stress_event,
                "event_ongoing": ongoing,
                "input_method": "feishu_card",
                "card_message_id": str(message_id)[:128],
            },
            source_message_id=event_id[:128],
        )
        return {
            "ok": True,
            "observation_id": str(observation_id),
            "reply_text": f"已记录这次状态：压力 {stress:g}/10，精力 {energy:g}/10。",
        }
