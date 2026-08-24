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
from app.services.observation_forecast_refresh import ObservationForecastRefreshService


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
        daily_reviews: Any = None,
        observation_refresh: ObservationForecastRefreshService,
        care_interventions: Any = None,
    ):
        self.observations = observations
        self.calendar = calendar
        self.timezone = ZoneInfo(timezone_name)
        self.daily_reviews = daily_reviews
        self.observation_refresh = observation_refresh
        self.care_interventions = care_interventions

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
        if action_name.startswith("care_"):
            if self.care_interventions is None:
                raise RuntimeError("care intervention service is unavailable")
            if str(action.get("version") or "") != "1":
                return {"ok": False, "error": "unsupported_card_action_version"}
            try:
                intervention_id = uuid.UUID(str(action.get("intervention_id") or ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("care intervention id is invalid") from exc
            event_id = str(callback_event_id or "").strip() or self._fallback_event_id(
                message_id, action, dict(form_value or {})
            )
            care_action = action_name.removeprefix("care_")
            result = self.care_interventions.apply_action(
                participant_id,
                intervention_id,
                action=care_action,
                callback_event_id=event_id,
            )
            reply = {
                "ack": "知道了，本次提醒已确认。",
                "helpful": "谢谢反馈，我已记录这条提醒有帮助。",
                "not_relevant": "谢谢反馈，我已记录这条提醒不太相关。",
                "mute_today": "已关闭今天剩余的主动关怀提醒。",
                "snooze_30": (
                    "已安排 30 分钟后再提醒。"
                    if result.get("action_result") == "scheduled"
                    else "已记录延后选择，但受当前提醒上限或设置限制，未新增提醒。"
                ),
            }.get(care_action, "关怀反馈已记录。")
            return {
                "ok": True,
                "care_intervention_id": str(intervention_id),
                "created": bool(result["created"]),
                "action_result": result.get("action_result"),
                "reply_text": reply,
            }
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
        if action_name == "daily_review_submit":
            if self.daily_reviews is None:
                raise RuntimeError("daily review service is unavailable")
            values = dict(form_value or {})
            event_id = str(callback_event_id or "").strip() or self._fallback_event_id(
                message_id, action, values
            )
            result = self.daily_reviews.submit(
                participant_id,
                callback_event_id=event_id,
                action=action,
                values=values,
            )
            response = result["response"]
            return {
                "ok": True,
                "daily_review_response_id": response["id"],
                "reply_text": (
                    "每日回顾已记录并生成回顾估计。"
                    if result["created"] else "这次每日回顾已记录，无需重复提交。"
                ),
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
        write = self.observations.add_with_status(
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
        self.observation_refresh.on_observation_committed(
            participant_id=participant_id,
            observed_at=write.observed_at,
            created=write.created,
        )
        return {
            "ok": True,
            "observation_id": str(write.observation_id),
            "reply_text": f"已记录这次状态：压力 {stress:g}/10，精力 {energy:g}/10。",
        }
