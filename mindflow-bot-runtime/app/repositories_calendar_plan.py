"""Durable participant-bound confirmation plans for batch Calendar mutations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from app.db import Database
from app.models import CalendarMutationPlan


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CalendarMutationPlanRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: CalendarMutationPlan) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "operation": row.operation,
            "items": [dict(item) for item in list(row.items_json or [])],
            "status": row.status,
            "result": dict(row.result_json or {}),
            "expires_at": _aware(row.expires_at).isoformat(),
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "completed_at": (
                _aware(row.completed_at).isoformat() if row.completed_at else None
            ),
        }

    def create(
        self,
        participant_id: uuid.UUID,
        *,
        operation: str,
        items: list[dict[str, Any]],
        ttl_minutes: int = 15,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_operation = str(operation).strip().lower()
        if normalized_operation not in {"create", "delete"}:
            raise ValueError("unsupported calendar mutation plan operation")
        if not 2 <= len(items) <= 20:
            raise ValueError("calendar mutation plan requires 2 to 20 items")
        created_at = _aware(now or datetime.now(timezone.utc))
        row = CalendarMutationPlan(
            participant_id=participant_id,
            operation=normalized_operation,
            items_json=[dict(item) for item in items],
            status="awaiting_confirmation",
            expires_at=created_at + timedelta(minutes=max(1, int(ttl_minutes))),
            created_at=created_at,
            updated_at=created_at,
        )
        with self.database.session() as session:
            session.add(row)
            session.flush()
            return self._view(row)

    def claim(
        self,
        participant_id: uuid.UUID,
        plan_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None or str(row.participant_id) != str(participant_id):
                return None
            if row.status == "awaiting_confirmation" and _aware(row.expires_at) <= claimed_at:
                row.status = "expired"
                row.updated_at = claimed_at
                row.completed_at = claimed_at
            if row.status != "awaiting_confirmation":
                value = self._view(row)
                value["claim_status"] = row.status
                return value
            row.status = "processing"
            row.updated_at = claimed_at
            session.flush()
            value = self._view(row)
            value["claim_status"] = "claimed"
            return value

    def cancel(
        self,
        participant_id: uuid.UUID,
        plan_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        cancelled_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None or str(row.participant_id) != str(participant_id):
                return None
            if row.status == "awaiting_confirmation":
                row.status = (
                    "expired"
                    if _aware(row.expires_at) <= cancelled_at
                    else "cancelled"
                )
                row.updated_at = cancelled_at
                row.completed_at = cancelled_at
            session.flush()
            return self._view(row)

    def complete(
        self,
        plan_id: uuid.UUID | str,
        *,
        result: dict[str, Any],
        succeeded: bool,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        completed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CalendarMutationPlan, uuid.UUID(str(plan_id)), with_for_update=True
            )
            if row is None or row.status != "processing":
                return None
            row.status = "succeeded" if succeeded else "partial_failed"
            row.result_json = dict(result)
            row.updated_at = completed_at
            row.completed_at = completed_at
            session.flush()
            return self._view(row)
