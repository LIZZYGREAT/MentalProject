"""Delivery record helper for user-requested MVP care cards."""

from __future__ import annotations

from datetime import date
from typing import Optional

from auth.database import AppDatabase
from services.onboarding import new_id


class CareDeliveryPolicy:
    """Record auditable requested messages; proactive scheduling is intentionally out of scope."""

    def __init__(self, database: AppDatabase):
        self.database = database

    def create_requested_delivery(
        self,
        *,
        user_id: int,
        request_event_id: str,
        prediction_run_id: Optional[str] = None,
        local_date: Optional[str] = None,
    ) -> dict:
        return self.database.create_care_delivery(
            delivery_id=new_id(),
            user_id=int(user_id),
            prediction_run_id=prediction_run_id,
            local_date=str(local_date or date.today().isoformat()),
            episode_key=f"requested:{request_event_id}",
            tier="support",
            channel="feishu",
            status="candidate",
        )

    def mark_sent(self, delivery_id: str, provider_message_id: str) -> bool:
        return self.database.mark_care_delivery_sent(delivery_id, provider_message_id)

    def mark_failed(self, delivery_id: str, reason: str) -> bool:
        return self.database.mark_care_delivery_failed(delivery_id, reason)
