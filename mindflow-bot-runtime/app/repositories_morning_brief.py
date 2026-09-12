"""Durable schedule state for deterministic morning briefs."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.models import MorningBriefSchedule


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class MorningBriefScheduleRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure(self, participant_id: uuid.UUID, local_date: date, scheduled_at: datetime, *, valid_until: datetime) -> dict[str, Any]:
        try:
            with self.database.session() as session:
                row = MorningBriefSchedule(
                    participant_id=participant_id, local_date=local_date,
                    scheduled_at=_aware(scheduled_at), valid_until=_aware(valid_until),
                )
                session.add(row)
                session.flush()
                return self._view(row)
        except IntegrityError:
            with self.database.session() as session:
                row = session.execute(select(MorningBriefSchedule).where(
                    MorningBriefSchedule.participant_id == participant_id,
                    MorningBriefSchedule.local_date == local_date,
                )).scalar_one()
                return self._view(row)

    def claim_due(self, now: datetime, lease_seconds: int, limit: int = 100) -> list[dict[str, Any]]:
        instant = _aware(now)
        with self.database.session() as session:
            ids = session.execute(select(MorningBriefSchedule.id).where(
                MorningBriefSchedule.scheduled_at <= instant,
                MorningBriefSchedule.valid_until > instant,
                or_(
                    MorningBriefSchedule.status == "pending",
                    (MorningBriefSchedule.status == "claimed") & (MorningBriefSchedule.lease_until <= instant),
                ),
                or_(MorningBriefSchedule.next_attempt_at.is_(None), MorningBriefSchedule.next_attempt_at <= instant),
            ).order_by(MorningBriefSchedule.scheduled_at).limit(max(1, limit))).scalars().all()
            claimed = []
            for row_id in ids:
                row = session.get(MorningBriefSchedule, row_id, with_for_update=True)
                if row is None:
                    continue
                token = uuid.uuid4()
                row.status = "claimed"
                row.claim_token = token
                row.lease_until = instant + timedelta(seconds=max(1, lease_seconds))
                row.attempt_count += 1
                row.updated_at = instant
                claimed.append(self._view(row))
            return claimed

    def finish(self, schedule_id: str, claim_token: str, *, status: str, now: datetime, provider_message_id: str | None = None, error_code: str | None = None, retry_after_seconds: int | None = None) -> bool:
        instant = _aware(now)
        with self.database.session() as session:
            row = session.execute(select(MorningBriefSchedule).where(
                MorningBriefSchedule.id == uuid.UUID(schedule_id),
                MorningBriefSchedule.claim_token == uuid.UUID(claim_token),
                MorningBriefSchedule.status == "claimed",
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            row.status = status
            row.claim_token = None
            row.lease_until = None
            row.provider_message_id = provider_message_id
            row.sent_at = instant if status == "sent" else None
            row.last_error_code = error_code
            row.next_attempt_at = instant + timedelta(seconds=retry_after_seconds) if retry_after_seconds is not None else None
            row.updated_at = instant
            return True

    @staticmethod
    def _view(row: MorningBriefSchedule) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(),
            "scheduled_at": _aware(row.scheduled_at).isoformat(),
            "valid_until": _aware(row.valid_until).isoformat(),
            "status": row.status, "attempt_count": int(row.attempt_count),
            "claim_token": str(row.claim_token) if row.claim_token else None,
        }
