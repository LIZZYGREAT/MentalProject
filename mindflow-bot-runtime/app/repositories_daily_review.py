"""Persistence boundaries for Daily Review delivery, revisions, and posterior curves."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
import uuid

from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.models import (
    DailyReviewResponse,
    DailyReviewSchedule,
    RetrospectiveCurveSnapshot,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class DailyReviewScheduleRepository:
    CARD_VERSION = "daily-review-v1"

    def __init__(self, database: Database):
        self.database = database

    def ensure(
        self, participant_id: uuid.UUID, local_date: date, scheduled_at: datetime,
        *, valid_until: datetime | None = None,
        card_version: str = CARD_VERSION,
    ) -> dict[str, Any]:
        scheduled = _aware(scheduled_at)
        validity_end = _aware(valid_until or (scheduled + timedelta(days=1)))
        if validity_end <= scheduled:
            raise ValueError("valid_until must be after scheduled_at")
        try:
            with self.database.session() as session:
                row = session.execute(select(DailyReviewSchedule).where(
                    DailyReviewSchedule.participant_id == participant_id,
                    DailyReviewSchedule.local_date == local_date,
                    DailyReviewSchedule.card_version == card_version,
                )).scalar_one_or_none()
                if row is None:
                    row = DailyReviewSchedule(
                        participant_id=participant_id,
                        local_date=local_date,
                        card_version=card_version,
                        scheduled_at=scheduled,
                        valid_until=validity_end,
                        next_attempt_at=scheduled,
                    )
                    session.add(row)
                    session.flush()
                return self._view(row)
        except IntegrityError:
            with self.database.session() as session:
                row = session.execute(select(DailyReviewSchedule).where(
                    DailyReviewSchedule.participant_id == participant_id,
                    DailyReviewSchedule.local_date == local_date,
                    DailyReviewSchedule.card_version == card_version,
                )).scalar_one()
                return self._view(row)

    @staticmethod
    def _expire(row: DailyReviewSchedule, now: datetime) -> None:
        row.status = "expired"
        row.next_attempt_at = None
        row.claim_token = None
        row.lease_until = None
        row.last_error_code = "delivery_window_expired"
        row.updated_at = now

    def reactivate_available(self, participant_id: uuid.UUID, now: datetime) -> None:
        now = _aware(now)
        with self.database.session() as session:
            rows = session.execute(select(DailyReviewSchedule).where(
                DailyReviewSchedule.participant_id == participant_id,
                DailyReviewSchedule.status == "delivery_unavailable",
            )).scalars().all()
            for row in rows:
                if _aware(row.valid_until) <= now:
                    self._expire(row, now)
                    continue
                row.status = "pending"
                row.next_attempt_at = now
                row.updated_at = now

    def claim_due(self, now: datetime, lease_seconds: int, limit: int = 100) -> list[dict[str, Any]]:
        now = _aware(now)
        claimed: list[dict[str, Any]] = []
        with self.database.session() as session:
            expired = session.execute(select(DailyReviewSchedule).where(
                DailyReviewSchedule.valid_until <= now,
                DailyReviewSchedule.status.in_((
                    "pending", "claimed", "delivery_unavailable"
                )),
            ).with_for_update()).scalars().all()
            for row in expired:
                self._expire(row, now)
            rows = session.execute(select(DailyReviewSchedule).where(
                DailyReviewSchedule.scheduled_at <= now,
                DailyReviewSchedule.valid_until > now,
                or_(
                    DailyReviewSchedule.status == "pending",
                    (
                        (DailyReviewSchedule.status == "claimed")
                        & (DailyReviewSchedule.lease_until <= now)
                    ),
                ),
                or_(
                    DailyReviewSchedule.next_attempt_at.is_(None),
                    DailyReviewSchedule.next_attempt_at <= now,
                ),
            ).order_by(DailyReviewSchedule.scheduled_at).limit(limit).with_for_update()).scalars().all()
            for row in rows:
                token = uuid.uuid4()
                row.status = "claimed"
                row.claimed_at = now
                row.lease_until = now + timedelta(seconds=lease_seconds)
                row.claim_token = token
                row.attempt_count += 1
                row.updated_at = now
                session.flush()
                claimed.append(self._view(row))
        return claimed

    def mark_sent(
        self, schedule_id: uuid.UUID | str, claim_token: uuid.UUID | str,
        *, now: datetime, provider_message_id: str | None,
    ) -> bool:
        return self._finish(
            schedule_id, claim_token, status="sent", now=now,
            provider_message_id=provider_message_id,
        )

    def mark_unavailable(
        self, schedule_id: uuid.UUID | str, claim_token: uuid.UUID | str, *, now: datetime
    ) -> bool:
        return self._finish(
            schedule_id, claim_token, status="delivery_unavailable", now=now,
            error_code="missing_active_chat_binding",
        )

    def mark_failed(
        self, schedule_id: uuid.UUID | str, claim_token: uuid.UUID | str, *, now: datetime,
        error: Exception, max_attempts: int, retry_base_seconds: int,
    ) -> bool:
        sid, token = uuid.UUID(str(schedule_id)), uuid.UUID(str(claim_token))
        now = _aware(now)
        with self.database.session() as session:
            row = session.execute(select(DailyReviewSchedule).where(
                DailyReviewSchedule.id == sid,
                DailyReviewSchedule.claim_token == token,
                DailyReviewSchedule.status == "claimed",
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            terminal = row.attempt_count >= max_attempts
            row.status = "failed" if terminal else "pending"
            row.last_error_code = "delivery_failed"
            row.last_error_class = type(error).__name__[:128]
            row.next_attempt_at = None if terminal else now + timedelta(
                seconds=retry_base_seconds * 2 ** max(0, row.attempt_count - 1)
            )
            row.claim_token = None
            row.lease_until = None
            row.updated_at = now
            return True

    def _finish(
        self, schedule_id: uuid.UUID | str, claim_token: uuid.UUID | str, *,
        status: str, now: datetime, provider_message_id: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        sid, token = uuid.UUID(str(schedule_id)), uuid.UUID(str(claim_token))
        now = _aware(now)
        with self.database.session() as session:
            row = session.execute(select(DailyReviewSchedule).where(
                DailyReviewSchedule.id == sid,
                DailyReviewSchedule.claim_token == token,
                DailyReviewSchedule.status == "claimed",
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            row.status = status
            row.sent_at = now if status == "sent" else None
            row.provider_message_id = str(provider_message_id)[:128] if provider_message_id else None
            row.last_error_code = error_code
            row.claim_token = None
            row.lease_until = None
            row.next_attempt_at = None
            row.updated_at = now
            return True

    def get(self, schedule_id: uuid.UUID | str) -> dict[str, Any] | None:
        try:
            sid = uuid.UUID(str(schedule_id))
        except ValueError:
            return None
        with self.database.session() as session:
            row = session.get(DailyReviewSchedule, sid)
            return self._view(row) if row else None

    @staticmethod
    def _view(row: DailyReviewSchedule) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(), "card_version": row.card_version,
            "scheduled_at": row.scheduled_at.isoformat(), "status": row.status,
            "valid_until": row.valid_until.isoformat(),
            "attempt_count": row.attempt_count,
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
            "claim_token": str(row.claim_token) if row.claim_token else None,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "provider_message_id": row.provider_message_id,
            "last_error_code": row.last_error_code,
            "last_error_class": row.last_error_class,
        }


class DailyReviewResponseRepository:
    def __init__(self, database: Database):
        self.database = database

    def add(
        self, participant_id: uuid.UUID, local_date: date, *, callback_event_id: str,
        submitted_at: datetime, card_version: str, schedule_id: uuid.UUID | None,
        values: dict[str, Any], raw: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        callback = str(callback_event_id)[:128]
        try:
            with self.database.session() as session:
                existing = session.execute(select(DailyReviewResponse).where(
                    DailyReviewResponse.participant_id == participant_id,
                    DailyReviewResponse.callback_event_id == callback,
                )).scalar_one_or_none()
                if existing:
                    return self._view(existing), False
                revision = (session.scalar(select(func.max(DailyReviewResponse.revision)).where(
                    DailyReviewResponse.participant_id == participant_id,
                    DailyReviewResponse.local_date == local_date,
                )) or 0) + 1
                row = DailyReviewResponse(
                    participant_id=participant_id, local_date=local_date,
                    revision=revision, card_version=card_version,
                    schedule_id=schedule_id, callback_event_id=callback,
                    submitted_at=_aware(submitted_at), raw_json=dict(raw), **values,
                )
                session.add(row)
                session.flush()
                return self._view(row), True
        except IntegrityError:
            with self.database.session() as session:
                row = session.execute(select(DailyReviewResponse).where(
                    DailyReviewResponse.participant_id == participant_id,
                    DailyReviewResponse.callback_event_id == callback,
                )).scalar_one_or_none()
                if row:
                    return self._view(row), False
            # A distinct callback may have won the same next revision. Retry
            # after rollback so it receives the following append-only number.
            return self.add(
                participant_id, local_date,
                callback_event_id=callback_event_id,
                submitted_at=submitted_at,
                card_version=card_version,
                schedule_id=schedule_id,
                values=values,
                raw=raw,
            )

    def latest(self, participant_id: uuid.UUID, local_date: date) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.execute(select(DailyReviewResponse).where(
                DailyReviewResponse.participant_id == participant_id,
                DailyReviewResponse.local_date == local_date,
            ).order_by(desc(DailyReviewResponse.revision)).limit(1)).scalar_one_or_none()
            return self._view(row) if row else None

    def list(self, participant_id: uuid.UUID, local_date: date | None = None) -> list[dict[str, Any]]:
        conditions = [DailyReviewResponse.participant_id == participant_id]
        if local_date is not None:
            conditions.append(DailyReviewResponse.local_date == local_date)
        with self.database.session() as session:
            rows = session.execute(select(DailyReviewResponse).where(*conditions).order_by(
                desc(DailyReviewResponse.local_date), desc(DailyReviewResponse.revision)
            )).scalars().all()
            return [self._view(row) for row in rows]

    @staticmethod
    def _view(row: DailyReviewResponse) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(), "revision": row.revision,
            "card_version": row.card_version,
            "schedule_id": str(row.schedule_id) if row.schedule_id else None,
            "callback_event_id": row.callback_event_id,
            "submitted_at": row.submitted_at.isoformat(),
            "start_stress": row.start_stress, "start_energy": row.start_energy,
            "peak_stress": row.peak_stress, "peak_period": row.peak_period,
            "end_stress": row.end_stress, "end_energy": row.end_energy,
            "energy_consumption": row.energy_consumption,
            "main_stressor": row.main_stressor, "recovery_note": row.recovery_note,
            "free_text": row.free_text, "raw": dict(row.raw_json),
        }


class RetrospectiveCurveRepository:
    def __init__(self, database: Database):
        self.database = database

    def save(self, participant_id: uuid.UUID, local_date: date, **values: Any) -> dict[str, Any]:
        try:
            with self.database.session() as session:
                existing = session.execute(select(RetrospectiveCurveSnapshot).where(
                    RetrospectiveCurveSnapshot.participant_id == participant_id,
                    RetrospectiveCurveSnapshot.local_date == local_date,
                    RetrospectiveCurveSnapshot.reconstruction_version == values["reconstruction_version"],
                )).scalar_one_or_none()
                if existing:
                    return self._view(existing)
                row = RetrospectiveCurveSnapshot(
                    participant_id=participant_id, local_date=local_date, **values
                )
                session.add(row)
                session.flush()
                return self._view(row)
        except IntegrityError:
            with self.database.session() as session:
                row = session.execute(select(RetrospectiveCurveSnapshot).where(
                    RetrospectiveCurveSnapshot.participant_id == participant_id,
                    RetrospectiveCurveSnapshot.local_date == local_date,
                    RetrospectiveCurveSnapshot.reconstruction_version == values["reconstruction_version"],
                )).scalar_one()
                return self._view(row)

    def latest(self, participant_id: uuid.UUID, local_date: date) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.execute(select(RetrospectiveCurveSnapshot).where(
                RetrospectiveCurveSnapshot.participant_id == participant_id,
                RetrospectiveCurveSnapshot.local_date == local_date,
            ).order_by(desc(RetrospectiveCurveSnapshot.generated_at)).limit(1)).scalar_one_or_none()
            return self._view(row) if row else None

    @staticmethod
    def _view(row: RetrospectiveCurveSnapshot) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(),
            "source_forecast_id": str(row.source_forecast_id),
            "source_forecast_version": row.source_forecast_version,
            "daily_review_response_id": str(row.daily_review_response_id),
            "daily_review_revision": row.daily_review_revision,
            "observation_revision": row.observation_revision,
            "algorithm_version": row.algorithm_version,
            "reconstruction_version": row.reconstruction_version,
            "curve": list(row.curve_json), "analysis": dict(row.analysis_json),
            "diagnostics": dict(row.diagnostics_json),
            "generated_at": row.generated_at.isoformat(),
        }
