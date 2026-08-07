"""Deterministic proactive Feishu care candidate selection.

This scheduler never asks a model to decide whether to contact a user.  It uses
fresh project predictions, explicit preferences, quiet hours, daily budgets,
and durable episode keys.  Message delivery remains in ``CareWorker``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from auth.database import AppDatabase
from integrations.feishu import cards
from services.care_delivery import CareDeliveryPolicy
from services.care_service import CareService
from services.onboarding import new_id


@dataclass(frozen=True)
class ProactiveCandidate:
    user_id: int
    chat_id: str
    content: Dict[str, Any]
    delivery_id: str
    kind: str


def _minutes(value: str) -> int:
    hour, minute = str(value).split(":", 1)
    return int(hour) * 60 + int(minute)


def _in_quiet_hours(now_hm: str, start: str, end: str) -> bool:
    current = _minutes(now_hm)
    quiet_start = _minutes(start)
    quiet_end = _minutes(end)
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= current < quiet_end
    return current >= quiet_start or current < quiet_end


class ProactiveCareScheduler:
    def __init__(
        self,
        database: AppDatabase,
        care_service: CareService,
        *,
        lead_minutes: int = 90,
    ):
        self.database = database
        self.care_service = care_service
        self.deliveries = CareDeliveryPolicy(database)
        self.lead_minutes = max(15, min(int(lead_minutes), 360))

    def next_candidate(self, now: Optional[datetime] = None) -> Optional[ProactiveCandidate]:
        now = now or datetime.now().astimezone()
        local_date = now.date().isoformat()
        now_hm = now.strftime("%H:%M")
        for user in self.database.list_proactive_feishu_users(limit=200):
            user_id = int(user["user_id"])
            if _in_quiet_hours(
                now_hm,
                str(user.get("quiet_start") or "23:00"),
                str(user.get("quiet_end") or "07:00"),
            ):
                continue
            if self.database.sent_care_delivery_count(user_id, local_date) >= int(
                user.get("max_daily_messages") or 0
            ):
                continue

            confirmation = self.care_service.get_event_confirmations(
                user_id, local_date, as_of=now.isoformat(timespec="minutes")
            )
            if confirmation.get("events"):
                first = confirmation["events"][0]
                episode = f"completion:{first['event_id']}"
                delivery = self.database.create_care_delivery(
                    delivery_id=new_id(),
                    user_id=user_id,
                    prediction_run_id=confirmation.get("prediction_run_id"),
                    local_date=local_date,
                    episode_key=episode,
                    tier="completion_check",
                    status="candidate",
                )
                if delivery.get("status") == "candidate":
                    return ProactiveCandidate(
                        user_id=user_id,
                        chat_id=str(user["chat_id"]),
                        content=cards.event_completion_card(
                            confirmation["prediction_run_id"], [first]
                        ),
                        delivery_id=str(delivery["delivery_id"]),
                        kind="completion_check",
                    )

            run = self.database.latest_prediction_run_for_date(user_id, local_date)
            if not run:
                try:
                    self.care_service.run_today_assessment(user_id, local_date)
                    run = self.database.latest_prediction_run_for_date(user_id, local_date)
                except Exception:
                    run = None
            if not run:
                continue
            peak = self._future_peak(run, now)
            if not peak:
                continue
            episode = f"forecast:{run['prediction_run_id']}:{peak['time']}"
            delivery = self.database.create_care_delivery(
                delivery_id=new_id(),
                user_id=user_id,
                prediction_run_id=run.get("prediction_run_id"),
                local_date=local_date,
                episode_key=episode,
                tier="preemptive",
                status="candidate",
            )
            if delivery.get("status") != "candidate":
                continue
            support = self.care_service.get_support(
                user_id,
                {
                    "has_prediction": True,
                    "stress_0_10": float(peak["S"]) / 10.0,
                    "vitality_0_10": float(peak.get("E", 50.0)) / 10.0,
                    "prediction_run_id": run.get("prediction_run_id"),
                },
            )
            text = (
                f"模型预计 {peak['time']} 前后负荷可能升高。"
                f"{support['text']}这是提前参考，不代表你之后一定会感到压力。"
            )
            return ProactiveCandidate(
                user_id=user_id,
                chat_id=str(user["chat_id"]),
                content=cards.feedback_card(text, str(delivery["delivery_id"])),
                delivery_id=str(delivery["delivery_id"]),
                kind="preemptive_care",
            )
        return None

    def _future_peak(self, run: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        start = _minutes(now.strftime("%H:%M"))
        end = start + self.lead_minutes
        future = [
            point
            for point in run.get("points") or []
            if start <= _minutes(str(point.get("time") or "00:00")) <= end
        ]
        if not future:
            return None
        peak = max(future, key=lambda point: float(point.get("S", 0.0)))
        threshold = float((run.get("result") or {}).get("stress_threshold", 70.0))
        if float(peak.get("S", 0.0)) < max(65.0, threshold):
            return None
        return dict(peak)
