"""Participant-bound durable reminder workflow."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from app.db import Database
from app.models import Participant, Reminder, utc_now


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ReminderRepository:
    def __init__(self, database: Database, *, timezone_name: str, active_limit: int = 50) -> None:
        self.database = database
        self.timezone = ZoneInfo(timezone_name)
        self.active_limit = max(1, int(active_limit))

    def create(self, participant_id: uuid.UUID, *, message: str, remind_at: datetime, recurrence_type: str = "none") -> dict[str, Any]:
        normalized = " ".join(str(message).split())
        if not 1 <= len(normalized) <= 500:
            raise ValueError("reminder message must contain 1 to 500 characters")
        recurrence = str(recurrence_type)
        if recurrence not in {"none", "daily", "weekly"}:
            raise ValueError("unsupported reminder recurrence")
        instant = _aware(remind_at).astimezone(timezone.utc)
        if instant <= utc_now():
            raise ValueError("reminder time must be in the future")
        with self.database.session() as session:
            if session.get(Participant, participant_id) is None:
                raise ValueError("participant does not exist")
            count = session.scalar(select(func.count()).select_from(Reminder).where(
                Reminder.participant_id == participant_id, Reminder.status == "active"
            )) or 0
            if count >= self.active_limit:
                raise ValueError("active reminder limit reached")
            local = instant.astimezone(self.timezone)
            row = Reminder(
                participant_id=participant_id, message=normalized,
                remind_at_utc=instant, next_fire_at=instant,
                recurrence_type=recurrence,
                weekday=local.isoweekday() if recurrence == "weekly" else None,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def list_active(self, participant_id: uuid.UUID) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(select(Reminder).where(
                Reminder.participant_id == participant_id, Reminder.status == "active"
            ).order_by(Reminder.next_fire_at).limit(self.active_limit)).scalars().all()
            return [self._view(row) for row in rows]

    def list_for_user(
        self, participant_id: uuid.UUID, *, failed_within_days: int = 7
    ) -> list[dict[str, Any]]:
        """Return active reminders plus recent terminal delivery failures."""

        active = self.list_active(participant_id)
        cutoff = utc_now() - timedelta(days=max(1, int(failed_within_days)))
        with self.database.session() as session:
            failed = session.execute(select(Reminder).where(
                Reminder.participant_id == participant_id,
                Reminder.status == "delivery_failed",
                Reminder.updated_at >= cutoff,
            ).order_by(Reminder.updated_at.desc()).limit(self.active_limit)).scalars().all()
            return active + [self._view(row) for row in failed]

    def cancel(self, participant_id: uuid.UUID, reminder_id: uuid.UUID) -> bool:
        reminder_id = uuid.UUID(str(reminder_id))
        with self.database.session() as session:
            row = session.execute(select(Reminder).where(
                Reminder.id == reminder_id, Reminder.participant_id == participant_id
            ).with_for_update()).scalar_one_or_none()
            if row is None or row.status != "active":
                return False
            row.status = "cancelled"
            row.next_fire_at = None
            row.claim_token = None
            row.lease_until = None
            row.updated_at = utc_now()
            return True

    def claim_due(self, now: datetime, lease_seconds: int, limit: int = 100) -> list[dict[str, Any]]:
        instant = _aware(now)
        with self.database.session() as session:
            ids = session.execute(select(Reminder.id).where(
                Reminder.status == "active", Reminder.next_fire_at <= instant,
                or_(
                    Reminder.next_attempt_at.is_(None),
                    Reminder.next_attempt_at <= instant,
                ),
                or_(Reminder.claim_token.is_(None), Reminder.lease_until <= instant),
            ).order_by(Reminder.next_fire_at).limit(max(1, limit))).scalars().all()
            results = []
            for reminder_id in ids:
                row = session.get(Reminder, reminder_id, with_for_update=True)
                if row is None:
                    continue
                row.claim_token = uuid.uuid4()
                row.lease_until = instant + timedelta(seconds=max(1, lease_seconds))
                row.updated_at = instant
                results.append(self._view(row))
            return results

    def mark_fired(self, reminder_id: str, claim_token: str, *, fired_at: datetime) -> bool:
        instant = _aware(fired_at)
        with self.database.session() as session:
            row = session.execute(select(Reminder).where(
                Reminder.id == uuid.UUID(reminder_id), Reminder.claim_token == uuid.UUID(claim_token),
                Reminder.status == "active",
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            row.last_fired_at = instant
            row.fired_count += 1
            row.attempt_count = 0
            row.next_attempt_at = None
            row.last_error_code = None
            row.claim_token = None
            row.lease_until = None
            if row.recurrence_type == "none":
                row.status = "done"
                row.next_fire_at = None
            else:
                local = _aware(row.next_fire_at).astimezone(self.timezone)
                days = 1 if row.recurrence_type == "daily" else 7
                next_local = datetime.combine(local.date() + timedelta(days=days), local.timetz().replace(tzinfo=None), self.timezone)
                row.next_fire_at = next_local.astimezone(timezone.utc)
            row.updated_at = instant
            return True

    def record_delivery_failure(
        self,
        reminder_id: str,
        claim_token: str,
        *,
        now: datetime,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> bool:
        instant = _aware(now)
        with self.database.session() as session:
            row = session.execute(select(Reminder).where(
                Reminder.id == uuid.UUID(reminder_id),
                Reminder.claim_token == uuid.UUID(claim_token),
                Reminder.status == "active",
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            row.attempt_count += 1
            row.last_error_code = str(error_code)[:128]
            row.claim_token = None
            row.lease_until = None
            if row.attempt_count >= max(1, int(max_attempts)):
                row.status = "delivery_failed"
                row.next_attempt_at = None
                row.next_fire_at = None
            else:
                delay = max(1, int(retry_base_seconds)) * (
                    2 ** max(0, row.attempt_count - 1)
                )
                row.next_attempt_at = instant + timedelta(seconds=delay)
            row.updated_at = instant
            return True

    def release(self, reminder_id: str, claim_token: str) -> bool:
        with self.database.session() as session:
            row = session.execute(select(Reminder).where(
                Reminder.id == uuid.UUID(reminder_id), Reminder.claim_token == uuid.UUID(claim_token)
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            row.claim_token = None
            row.lease_until = None
            row.updated_at = utc_now()
            return True

    def for_local_date(self, participant_id: uuid.UUID, local_date: date) -> list[dict[str, Any]]:
        start = datetime.combine(local_date, time.min, self.timezone).astimezone(timezone.utc)
        end = (datetime.combine(local_date, time.min, self.timezone) + timedelta(days=1)).astimezone(timezone.utc)
        with self.database.session() as session:
            rows = session.execute(select(Reminder).where(
                Reminder.participant_id == participant_id, Reminder.status == "active",
                Reminder.next_fire_at >= start, Reminder.next_fire_at < end,
            ).order_by(Reminder.next_fire_at)).scalars().all()
            return [self._view(row) for row in rows]

    @staticmethod
    def _view(row: Reminder) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "message": row.message, "remind_at": _aware(row.remind_at_utc).isoformat(),
            "next_fire_at": _aware(row.next_fire_at).isoformat() if row.next_fire_at else None,
            "recurrence_type": row.recurrence_type, "weekday": row.weekday,
            "status": row.status, "fired_count": int(row.fired_count),
            "attempt_count": int(row.attempt_count),
            "next_attempt_at": (
                _aware(row.next_attempt_at).isoformat()
                if row.next_attempt_at else None
            ),
            "last_error_code": row.last_error_code,
            "claim_token": str(row.claim_token) if row.claim_token else None,
        }
