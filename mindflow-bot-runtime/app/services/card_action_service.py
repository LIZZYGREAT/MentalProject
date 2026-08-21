"""Fixed, participant-bound handlers for Feishu card actions.

Card callbacks never enter the language model.  The action allowlist and field
validation here are the authority for any state change triggered by a card.
"""

from __future__ import annotations

import uuid
from typing import Any

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
    def __init__(self, observations: ObservationRepository):
        self.observations = observations

    def handle(
        self,
        participant_id: uuid.UUID,
        *,
        message_id: str,
        action_value: dict[str, Any] | None,
        form_value: dict[str, Any] | None,
    ) -> dict[str, Any]:
        action = dict(action_value or {})
        if (
            action.get("mindflow_action") != "submit_checkin"
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
            },
            source_message_id=f"card:{str(message_id)[:128]}",
        )
        return {
            "ok": True,
            "observation_id": str(observation_id),
            "reply_text": f"已记录这次状态：压力 {stress:g}/10，精力 {energy:g}/10。",
        }
