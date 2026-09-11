"""Durable participant-bound course-schedule image sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from sqlalchemy import select

from app.db import Database
from app.models import CourseScheduleImageSession, CourseScheduleImport


ACTIVE_IMAGE_SESSION_STATUSES = frozenset(
    {"parsing", "needs_information", "needs_retry", "ready"}
)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CourseScheduleImageSessionRepository:
    def __init__(self, database: Database, *, ttl_hours: int = 24) -> None:
        self.database = database
        self.ttl_hours = max(1, min(int(ttl_hours), 168))

    def start(
        self,
        participant_id: uuid.UUID,
        *,
        chat_id: str,
        image_message_id: str,
        image_key: str,
        vision_model: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        started_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.execute(
                select(CourseScheduleImageSession)
                .where(
                    CourseScheduleImageSession.participant_id == participant_id,
                    CourseScheduleImageSession.image_message_id
                    == str(image_message_id)[:128],
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                row = CourseScheduleImageSession(
                    participant_id=participant_id,
                    chat_id=str(chat_id)[:128],
                    image_message_id=str(image_message_id)[:128],
                    image_key=str(image_key)[:512],
                    status="parsing",
                    vision_model=(str(vision_model)[:128] if vision_model else None),
                    created_at=started_at,
                    updated_at=started_at,
                    expires_at=started_at + timedelta(hours=self.ttl_hours),
                )
                session.add(row)
            else:
                row.chat_id = str(chat_id)[:128]
                row.image_key = str(image_key)[:512]
                row.status = "parsing"
                row.last_error_code = None
                row.error_detail = None
                row.updated_at = started_at
                row.expires_at = started_at + timedelta(hours=self.ttl_hours)
                if vision_model:
                    row.vision_model = str(vision_model)[:128]
            session.flush()
            return self._view(row)

    def mark_ready(
        self,
        participant_id: uuid.UUID,
        image_message_id: str,
        *,
        import_id: uuid.UUID | str | None = None,
        needs_information: bool = False,
        parse_report: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._update(
            participant_id,
            image_message_id,
            status="needs_information" if needs_information else "ready",
            import_id=import_id,
            parse_report=parse_report,
            error_code=None,
            error_detail=None,
            now=now,
        )

    def mark_failed(
        self,
        participant_id: uuid.UUID,
        image_message_id: str,
        *,
        error_code: str,
        error_detail: str | None = None,
        parse_report: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._update(
            participant_id,
            image_message_id,
            status="needs_retry",
            import_id=None,
            parse_report=parse_report,
            error_code=error_code,
            error_detail=error_detail,
            now=now,
        )

    def archive(
        self,
        participant_id: uuid.UUID,
        image_message_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._update(
            participant_id,
            image_message_id,
            status="archived",
            now=now,
        )

    def latest(
        self,
        participant_id: uuid.UUID,
        *,
        chat_id: str | None = None,
        include_archived: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        checked_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            self._archive_expired(session, participant_id, checked_at)
            query = select(CourseScheduleImageSession).where(
                CourseScheduleImageSession.participant_id == participant_id
            )
            if chat_id is not None:
                query = query.where(
                    CourseScheduleImageSession.chat_id == str(chat_id)[:128]
                )
            if not include_archived:
                query = query.where(
                    CourseScheduleImageSession.status != "archived"
                )
            row = session.execute(
                query.order_by(
                    CourseScheduleImageSession.updated_at.desc(),
                    CourseScheduleImageSession.id.desc(),
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            self._sync_import_status(session, row, checked_at)
            session.flush()
            return self._view(row)

    def last_failure(
        self,
        participant_id: uuid.UUID,
        *,
        chat_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        checked_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            self._archive_expired(session, participant_id, checked_at)
            query = select(CourseScheduleImageSession).where(
                CourseScheduleImageSession.participant_id == participant_id,
                CourseScheduleImageSession.status == "needs_retry",
            )
            if chat_id is not None:
                query = query.where(
                    CourseScheduleImageSession.chat_id == str(chat_id)[:128]
                )
            row = session.execute(
                query.order_by(CourseScheduleImageSession.updated_at.desc()).limit(1)
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def _update(
        self,
        participant_id: uuid.UUID,
        image_message_id: str,
        *,
        status: str,
        import_id: uuid.UUID | str | None = None,
        parse_report: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        updated_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.execute(
                select(CourseScheduleImageSession)
                .where(
                    CourseScheduleImageSession.participant_id == participant_id,
                    CourseScheduleImageSession.image_message_id
                    == str(image_message_id)[:128],
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise LookupError("course schedule image session not found")
            row.status = status
            if import_id is not None:
                row.import_id = uuid.UUID(str(import_id))
            if parse_report is not None:
                row.parse_report_json = dict(parse_report)
            row.last_error_code = (
                str(error_code)[:128] if error_code is not None else None
            )
            row.error_detail = (
                str(error_detail)[:500] if error_detail is not None else None
            )
            row.updated_at = updated_at
            session.flush()
            return self._view(row)

    @staticmethod
    def _archive_expired(session: Any, participant_id: uuid.UUID, now: datetime) -> None:
        rows = list(
            session.execute(
                select(CourseScheduleImageSession)
                .where(
                    CourseScheduleImageSession.participant_id == participant_id,
                    CourseScheduleImageSession.status.in_(
                        ACTIVE_IMAGE_SESSION_STATUSES
                    ),
                    CourseScheduleImageSession.expires_at <= now,
                )
                .with_for_update()
            ).scalars()
        )
        for row in rows:
            row.status = "archived"
            row.updated_at = now
        if rows:
            session.flush()

    @staticmethod
    def _sync_import_status(
        session: Any, row: CourseScheduleImageSession, now: datetime
    ) -> None:
        if row.import_id is None:
            return
        schedule_import = session.get(CourseScheduleImport, row.import_id)
        if schedule_import is None:
            return
        if schedule_import.status in {"succeeded", "partial_failed"}:
            row.status = "imported"
            row.updated_at = now
        elif schedule_import.status in {"cancelled", "expired"}:
            row.status = "archived"
            row.updated_at = now

    @staticmethod
    def _view(row: CourseScheduleImageSession) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "import_id": str(row.import_id) if row.import_id else None,
            "chat_id": row.chat_id,
            "image_message_id": row.image_message_id,
            "image_key": row.image_key,
            "status": row.status,
            "parse_report": dict(row.parse_report_json or {}),
            "last_error_code": row.last_error_code,
            "error_detail": row.error_detail,
            "vision_model": row.vision_model,
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "expires_at": _aware(row.expires_at).isoformat(),
        }
