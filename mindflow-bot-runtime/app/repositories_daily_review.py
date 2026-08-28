"""Persistence boundaries for Daily Review delivery, revisions, and posterior curves."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.models import (
    DailyReviewResponse,
    DailyReviewSchedule,
    Participant,
    ParticipantCarePreference,
    RetrospectiveCurveSnapshot,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


_SQLITE_UNIQUE_SIGNATURES = {
    "uq_daily_review_schedule_version": (
        "daily_review_schedules.participant_id",
        "daily_review_schedules.local_date",
        "daily_review_schedules.card_version",
    ),
    "uq_daily_review_callback": (
        "daily_review_responses.participant_id",
        "daily_review_responses.callback_event_id",
    ),
    "uq_daily_review_revision": (
        "daily_review_responses.participant_id",
        "daily_review_responses.local_date",
        "daily_review_responses.revision",
    ),
    "uq_retrospective_reconstruction_version": (
        "retrospective_curve_snapshots.participant_id",
        "retrospective_curve_snapshots.local_date",
        "retrospective_curve_snapshots.reconstruction_version",
    ),
}


def _is_constraint(exc: IntegrityError, expected: str) -> bool:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return str(constraint_name) == expected
    message = str(original or exc).casefold()
    if expected.casefold() in message:
        return True
    signature = _SQLITE_UNIQUE_SIGNATURES.get(expected, ())
    return bool(signature) and all(column.casefold() in message for column in signature)


class DailyReviewRevisionConflict(RuntimeError):
    """Concurrent revision allocation did not converge within the retry bound."""


class DailyReviewScheduleRepository:
    CARD_VERSION = "daily-review-v1"

    def __init__(
        self,
        database: Database,
        *,
        timezone_name: str = "Asia/Shanghai",
    ):
        self.database = database
        self.timezone = ZoneInfo(timezone_name)

    def _hard_authorization_reason(
        self,
        preference: ParticipantCarePreference | None,
        now: datetime,
    ) -> str | None:
        if preference is None:
            return None
        if not bool(preference.care_enabled):
            return "care_disabled"
        if not bool(preference.daily_review_enabled):
            return "daily_review_disabled"
        if preference.muted_until is not None and _aware(preference.muted_until) > now:
            return "muted"
        start = preference.quiet_hours_start
        end = preference.quiet_hours_end
        if start is not None and end is not None and start != end:
            local_clock = now.astimezone(self.timezone).time().replace(tzinfo=None)
            in_quiet_hours = (
                start <= local_clock < end
                if start < end
                else local_clock >= start or local_clock < end
            )
            if in_quiet_hours:
                return "quiet_hours"
        return None

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
                participant = session.get(
                    Participant, participant_id, with_for_update=True
                )
                if participant is None:
                    raise ValueError("participant does not exist")
                preference = session.get(
                    ParticipantCarePreference, participant_id
                )
                authorization_reason = (
                    "inactive"
                    if participant.status != "active"
                    else self._hard_authorization_reason(preference, scheduled)
                )
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
                        status=(
                            "cancelled"
                            if authorization_reason is not None else "pending"
                        ),
                        next_attempt_at=(
                            None
                            if authorization_reason is not None else scheduled
                        ),
                        last_error_code=(
                            f"participant_{authorization_reason}"
                            if authorization_reason is not None else None
                        ),
                    )
                    session.add(row)
                    session.flush()
                elif (
                    authorization_reason is not None
                    and row.status in {
                        "pending", "claimed", "delivery_unavailable"
                    }
                    and row.authorized_at is None
                ):
                    row.status = "cancelled"
                    row.claimed_at = None
                    row.lease_until = None
                    row.claim_token = None
                    row.next_attempt_at = None
                    row.last_error_code = f"participant_{authorization_reason}"
                    row.last_error_class = None
                    row.updated_at = scheduled
                elif (
                    authorization_reason is None
                    and row.status == "cancelled"
                    and str(row.last_error_code or "") in {
                        "participant_care_disabled",
                        "participant_daily_review_disabled",
                        "participant_muted",
                        "participant_quiet_hours",
                        "participant_care_preference",
                    }
                    and _aware(row.valid_until) > datetime.now(timezone.utc)
                ):
                    row.status = "pending"
                    row.next_attempt_at = row.scheduled_at
                    row.last_error_code = None
                    row.last_error_class = None
                    row.updated_at = datetime.now(timezone.utc)
                if (
                    row.authorized_at is None
                    and row.sent_at is None
                    and row.status == "pending"
                    and row.attempt_count == 0
                ):
                    row.scheduled_at = scheduled
                    row.valid_until = validity_end
                    row.next_attempt_at = scheduled
                return self._view(row)
        except IntegrityError as exc:
            if not _is_constraint(exc, "uq_daily_review_schedule_version"):
                raise
            with self.database.session() as session:
                row = session.execute(select(DailyReviewSchedule).where(
                    DailyReviewSchedule.participant_id == participant_id,
                    DailyReviewSchedule.local_date == local_date,
                    DailyReviewSchedule.card_version == card_version,
                )).scalar_one_or_none()
                if row is None:
                    raise
                return self._view(row)

    @staticmethod
    def _expire(row: DailyReviewSchedule, now: datetime) -> None:
        row.status = "expired"
        row.next_attempt_at = None
        row.claim_token = None
        row.lease_until = None
        row.authorized_at = None
        row.last_error_code = "delivery_window_expired"
        row.last_error_class = None
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
        # Expiry is an independent transaction so the subsequent claim path
        # always acquires Participant before Schedule, matching preference
        # updates and avoiding cross-feature lock inversion.
        with self.database.session() as session:
            expired = session.execute(select(DailyReviewSchedule).where(
                DailyReviewSchedule.valid_until <= now,
                DailyReviewSchedule.status.in_((
                    "pending", "claimed", "delivery_unavailable"
                )),
            ).with_for_update()).scalars().all()
            for row in expired:
                self._expire(row, now)

        with self.database.session() as session:
            candidates = session.execute(select(DailyReviewSchedule.id).where(
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
            ).order_by(DailyReviewSchedule.scheduled_at).limit(limit)).scalars().all()

        for schedule_id in candidates:
            with self.database.session() as session:
                candidate = session.get(DailyReviewSchedule, schedule_id)
                if candidate is None:
                    continue
                participant = session.get(
                    Participant, candidate.participant_id, with_for_update=True
                )
                row = session.get(DailyReviewSchedule, schedule_id, with_for_update=True)
                if row is None or _aware(row.valid_until) <= now:
                    if row is not None and row.status in {
                        "pending", "claimed", "delivery_unavailable"
                    }:
                        self._expire(row, now)
                    continue
                claimable = (
                    row.status == "pending"
                    or (
                        row.status == "claimed"
                        and row.lease_until is not None
                        and _aware(row.lease_until) <= now
                    )
                )
                if (
                    not claimable
                    or _aware(row.scheduled_at) > now
                    or (
                        row.next_attempt_at is not None
                        and _aware(row.next_attempt_at) > now
                    )
                ):
                    continue
                preference = session.get(
                    ParticipantCarePreference, row.participant_id
                )
                authorization_reason = (
                    "inactive"
                    if participant is None or participant.status != "active"
                    else self._hard_authorization_reason(preference, now)
                )
                if authorization_reason is not None:
                    row.status = "cancelled"
                    row.claimed_at = None
                    row.lease_until = None
                    row.claim_token = None
                    row.authorized_at = None
                    row.next_attempt_at = None
                    row.last_error_code = f"participant_{authorization_reason}"
                    row.last_error_class = None
                    row.updated_at = now
                    continue
                token = uuid.uuid4()
                row.status = "claimed"
                row.claimed_at = now
                row.lease_until = now + timedelta(seconds=lease_seconds)
                row.claim_token = token
                row.authorized_at = None
                row.attempt_count += 1
                row.updated_at = now
                session.flush()
                claimed.append(self._view(row))
        return claimed

    def authorize_claim_current(
        self,
        schedule_id: uuid.UUID | str,
        claim_token: uuid.UUID | str,
        *,
        now: datetime,
    ) -> bool:
        sid, token = uuid.UUID(str(schedule_id)), uuid.UUID(str(claim_token))
        now = _aware(now)
        with self.database.session() as session:
            candidate = session.get(DailyReviewSchedule, sid)
            if candidate is None:
                return False
            participant = session.get(
                Participant, candidate.participant_id, with_for_update=True
            )
            row = session.get(DailyReviewSchedule, sid, with_for_update=True)
            if (
                row is None
                or row.status != "claimed"
                or row.claim_token != token
                or row.lease_until is None
                or _aware(row.lease_until) < now
                or _aware(row.valid_until) <= now
            ):
                return False
            preference = session.get(
                ParticipantCarePreference, row.participant_id
            )
            authorization_reason = (
                "inactive"
                if participant is None or participant.status != "active"
                else self._hard_authorization_reason(preference, now)
            )
            if authorization_reason is not None:
                row.status = "cancelled"
                row.claimed_at = None
                row.lease_until = None
                row.claim_token = None
                row.authorized_at = None
                row.next_attempt_at = None
                row.last_error_code = f"participant_{authorization_reason}"
                row.last_error_class = None
                row.updated_at = now
                return False
            row.authorized_at = now
            row.updated_at = now
            return True

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

    def mark_cancelled(
        self,
        schedule_id: uuid.UUID | str,
        claim_token: uuid.UUID | str,
        *,
        now: datetime,
        error_code: str,
    ) -> bool:
        return self._finish(
            schedule_id,
            claim_token,
            status="cancelled",
            now=now,
            error_code=error_code,
        )

    def defer_missing_forecast(
        self,
        schedule_id: uuid.UUID | str,
        claim_token: uuid.UUID | str,
        *,
        now: datetime,
        retry_after_seconds: int,
    ) -> bool:
        """Return a claimed schedule to pending without spending send retries."""

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
            row.status = "pending"
            row.attempt_count = max(0, row.attempt_count - 1)
            row.next_attempt_at = now + timedelta(
                seconds=max(1, int(retry_after_seconds))
            )
            row.claim_token = None
            row.lease_until = None
            row.authorized_at = None
            row.last_error_code = "source_forecast_unavailable"
            row.last_error_class = None
            row.updated_at = now
            return True

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
            retry_at = now + timedelta(
                seconds=retry_base_seconds * 2 ** max(0, row.attempt_count - 1)
            )
            expired = not terminal and retry_at >= _aware(row.valid_until)
            row.status = "failed" if terminal else "expired" if expired else "pending"
            row.last_error_code = "delivery_failed"
            row.last_error_class = type(error).__name__[:128]
            row.next_attempt_at = None if terminal or expired else retry_at
            row.claim_token = None
            row.lease_until = None
            row.authorized_at = None
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
            if status == "sent" and (
                row.authorized_at is None
                or now < _aware(row.authorized_at)
            ):
                return False
            row.status = status
            row.sent_at = now if status == "sent" else None
            row.provider_message_id = str(provider_message_id)[:128] if provider_message_id else None
            row.last_error_code = error_code
            row.last_error_class = None
            row.claim_token = None
            row.lease_until = None
            if status != "sent":
                row.authorized_at = None
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
            "authorized_at": (
                row.authorized_at.isoformat() if row.authorized_at else None
            ),
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "provider_message_id": row.provider_message_id,
            "last_error_code": row.last_error_code,
            "last_error_class": row.last_error_class,
            "updated_at": row.updated_at.isoformat(),
        }


class DailyReviewResponseRepository:
    MAX_REVISION_RETRIES = 5

    def __init__(self, database: Database):
        self.database = database

    def get_by_callback_event_id(
        self, participant_id: uuid.UUID, callback_event_id: str
    ) -> dict[str, Any] | None:
        callback = str(callback_event_id)[:128]
        with self.database.session() as session:
            row = session.execute(select(DailyReviewResponse).where(
                DailyReviewResponse.participant_id == participant_id,
                DailyReviewResponse.callback_event_id == callback,
            )).scalar_one_or_none()
            return self._view(row) if row else None

    def add(
        self, participant_id: uuid.UUID, local_date: date, *, callback_event_id: str,
        submitted_at: datetime, card_version: str, schedule_id: uuid.UUID | None,
        causal_source_forecast_id: uuid.UUID | str | None,
        causal_source_forecast_version: str | None,
        values: dict[str, Any], raw: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        callback = str(callback_event_id)[:128]
        last_conflict: IntegrityError | None = None
        for _attempt in range(self.MAX_REVISION_RETRIES):
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
                        causal_source_forecast_id=(
                            uuid.UUID(str(causal_source_forecast_id))
                            if causal_source_forecast_id is not None
                            else None
                        ),
                        causal_source_forecast_version=(
                            str(causal_source_forecast_version)
                            if causal_source_forecast_version is not None
                            else None
                        ),
                        submitted_at=_aware(submitted_at), raw_json=dict(raw), **values,
                    )
                    session.add(row)
                    session.flush()
                    return self._view(row), True
            except IntegrityError as exc:
                with self.database.session() as session:
                    row = session.execute(select(DailyReviewResponse).where(
                        DailyReviewResponse.participant_id == participant_id,
                        DailyReviewResponse.callback_event_id == callback,
                    )).scalar_one_or_none()
                    if row:
                        return self._view(row), False
                if not _is_constraint(exc, "uq_daily_review_revision"):
                    raise
                last_conflict = exc
        raise DailyReviewRevisionConflict(
            "daily review revision allocation exhausted bounded retries"
        ) from last_conflict

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
            "causal_source_forecast_id": (
                str(row.causal_source_forecast_id)
                if row.causal_source_forecast_id else None
            ),
            "causal_source_forecast_version": (
                row.causal_source_forecast_version
            ),
            "callback_event_id": row.callback_event_id,
            "submitted_at": row.submitted_at.isoformat(),
            "start_stress": row.start_stress, "start_energy": row.start_energy,
            "peak_stress": row.peak_stress, "peak_period": row.peak_period,
            "end_stress": row.end_stress, "end_energy": row.end_energy,
            "energy_consumption": row.energy_consumption,
            "peak_consistency": row.peak_stress >= max(
                row.start_stress, row.end_stress
            ),
            "main_stressor": row.main_stressor, "recovery_note": row.recovery_note,
            "free_text": row.free_text, "raw": dict(row.raw_json),
        }


class RetrospectiveCurveRepository:
    def __init__(self, database: Database):
        self.database = database

    def save(
        self, participant_id: uuid.UUID, local_date: date, **values: Any
    ) -> tuple[dict[str, Any], bool]:
        try:
            with self.database.session() as session:
                existing = session.execute(select(RetrospectiveCurveSnapshot).where(
                    RetrospectiveCurveSnapshot.participant_id == participant_id,
                    RetrospectiveCurveSnapshot.local_date == local_date,
                    RetrospectiveCurveSnapshot.reconstruction_version == values["reconstruction_version"],
                )).scalar_one_or_none()
                if existing:
                    return self._view(existing), False
                row = RetrospectiveCurveSnapshot(
                    participant_id=participant_id, local_date=local_date, **values
                )
                session.add(row)
                session.flush()
                return self._view(row), True
        except IntegrityError as exc:
            if not _is_constraint(
                exc, "uq_retrospective_reconstruction_version"
            ):
                raise
            with self.database.session() as session:
                row = session.execute(select(RetrospectiveCurveSnapshot).where(
                    RetrospectiveCurveSnapshot.participant_id == participant_id,
                    RetrospectiveCurveSnapshot.local_date == local_date,
                    RetrospectiveCurveSnapshot.reconstruction_version == values["reconstruction_version"],
                )).scalar_one_or_none()
                if row is None:
                    raise
                return self._view(row), False

    def latest(self, participant_id: uuid.UUID, local_date: date) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.execute(select(RetrospectiveCurveSnapshot).where(
                RetrospectiveCurveSnapshot.participant_id == participant_id,
                RetrospectiveCurveSnapshot.local_date == local_date,
            ).order_by(
                desc(RetrospectiveCurveSnapshot.daily_review_revision),
                desc(RetrospectiveCurveSnapshot.generated_at),
                desc(RetrospectiveCurveSnapshot.id),
            ).limit(1)).scalar_one_or_none()
            return self._view(row) if row else None

    def latest_for_response(
        self,
        participant_id: uuid.UUID,
        daily_review_response_id: uuid.UUID | str,
    ) -> dict[str, Any] | None:
        try:
            response_id = uuid.UUID(str(daily_review_response_id))
        except ValueError:
            return None
        with self.database.session() as session:
            row = session.execute(select(RetrospectiveCurveSnapshot).where(
                RetrospectiveCurveSnapshot.participant_id == participant_id,
                RetrospectiveCurveSnapshot.daily_review_response_id == response_id,
            ).order_by(
                desc(RetrospectiveCurveSnapshot.generated_at),
                desc(RetrospectiveCurveSnapshot.id),
            ).limit(1)).scalar_one_or_none()
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
            "analysis_kind": str(
                (row.diagnostics_json or {}).get("analysis_kind") or "causal"
            ),
            "generated_at": row.generated_at.isoformat(),
        }
