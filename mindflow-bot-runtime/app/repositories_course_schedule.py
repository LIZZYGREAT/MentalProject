"""Durable repository for participant-owned course schedule import drafts."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.contracts.course_schedule import ScheduleVisionResult
from app.db import Database
from app.models import CourseScheduleImport, CourseScheduleImportItem


ACTIVE_DRAFT_STATUSES = {"pending_context", "pending_confirmation", "partial_failed"}


class CourseScheduleImportRepository:
    def __init__(self, database: Database):
        self.database = database

    def create_draft(
        self,
        participant_id: uuid.UUID,
        *,
        source_message_id: str,
        source_image_hash: str,
        vision_model: str,
        result: ScheduleVisionResult,
        timezone_name: str,
        ttl_minutes: int = 60,
        semester_start_date: date | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        created_at = _aware(now or datetime.now(timezone.utc))
        missing = required_context(result, semester_start_date=semester_start_date)
        structured = result.to_dict()
        structured["missing_context"] = sorted(missing)
        row = CourseScheduleImport(
            participant_id=participant_id,
            source_message_id=str(source_message_id)[:128],
            source_image_hash=str(source_image_hash)[:64],
            status="pending_context" if missing else "pending_confirmation",
            semester_start_date=semester_start_date,
            timezone=str(timezone_name)[:64],
            vision_model=str(vision_model)[:128],
            structured_result=structured,
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=max(1, int(ttl_minutes))),
        )
        try:
            with self.database.session() as session:
                session.add(row)
                session.flush()
                for index, course in enumerate(result.courses):
                    key_payload = {
                        "index": index,
                        "course": course.course_name,
                        "weekday": course.weekday,
                        "period": [course.period_start, course.period_end],
                        "time": [course.start_time, course.end_time],
                        "location": course.location,
                        "week_rule": {
                            "start_week": course.week_rule.start_week,
                            "end_week": course.week_rule.end_week,
                            "odd_even": course.week_rule.odd_even,
                            "explicit_weeks": course.week_rule.explicit_weeks,
                        },
                    }
                    normalized_key = hashlib.sha256(
                        json.dumps(
                            key_payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), default=list,
                        ).encode("utf-8")
                    ).hexdigest()
                    session.add(
                        CourseScheduleImportItem(
                            import_id=row.id,
                            item_index=index,
                            course_name=course.course_name,
                            weekday=course.weekday,
                            start_time=_parse_time(course.start_time),
                            end_time=_parse_time(course.end_time),
                            location=course.location,
                            week_rule_json=key_payload["week_rule"],
                            normalized_key=normalized_key,
                            status="pending",
                        )
                    )
                session.flush()
                return self._view(session, row)
        except IntegrityError:
            existing = self.get_by_source(participant_id, source_message_id)
            if existing is None:
                raise
            return existing

    def get(self, import_id: uuid.UUID | str) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.get(CourseScheduleImport, uuid.UUID(str(import_id)))
            return self._view(session, row) if row is not None else None

    def get_by_source(
        self, participant_id: uuid.UUID, source_message_id: str
    ) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.execute(
                select(CourseScheduleImport).where(
                    CourseScheduleImport.participant_id == participant_id,
                    CourseScheduleImport.source_message_id == str(source_message_id)[:128],
                )
            ).scalar_one_or_none()
            return self._view(session, row) if row is not None else None

    def latest_pending_context(self, participant_id: uuid.UUID) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self.database.session() as session:
            row = session.execute(
                select(CourseScheduleImport).where(
                    CourseScheduleImport.participant_id == participant_id,
                    CourseScheduleImport.status == "pending_context",
                    CourseScheduleImport.expires_at > now,
                ).order_by(CourseScheduleImport.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            return self._view(session, row) if row is not None else None

    def set_semester_start_date(
        self, participant_id: uuid.UUID, import_id: uuid.UUID | str, value: date
    ) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session)
            if row.status not in ACTIVE_DRAFT_STATUSES:
                raise ValueError("draft no longer accepts context")
            row.semester_start_date = value
            structured = dict(row.structured_result or {})
            missing = set(structured.get("missing_context") or [])
            missing.discard("semester_start_date")
            structured["missing_context"] = sorted(missing)
            row.structured_result = structured
            row.status = "pending_context" if missing else "pending_confirmation"
            session.flush()
            return self._view(session, row)

    def set_period_time_mapping(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        mapping: dict[tuple[int, int], tuple[time, time]],
    ) -> dict[str, Any]:
        if not mapping:
            raise ValueError("period time mapping is empty")
        if any(end <= start for start, end in mapping.values()):
            raise ValueError("period end time must be after start time")
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session)
            if row.status not in ACTIVE_DRAFT_STATUSES:
                raise ValueError("draft no longer accepts context")
            structured = dict(row.structured_result or {})
            courses = [dict(value) for value in structured.get("courses") or []]
            items = self._items(session, row.id)
            for course, item in zip(courses, items):
                key = (course.get("period_start"), course.get("period_end"))
                if key in mapping:
                    start_clock, end_clock = mapping[key]
                    course["start_time"] = start_clock.strftime("%H:%M")
                    course["end_time"] = end_clock.strftime("%H:%M")
                    item.start_time = start_clock
                    item.end_time = end_clock
            missing = set(structured.get("missing_context") or [])
            if all(course.get("start_time") and course.get("end_time") for course in courses):
                missing.discard("period_time_mapping")
                missing.discard("actual_time")
            structured["courses"] = courses
            structured["missing_context"] = sorted(missing)
            row.structured_result = structured
            row.status = "pending_context" if missing else "pending_confirmation"
            session.flush()
            return self._view(session, row)

    def begin_confirmation(
        self, participant_id: uuid.UUID, import_id: uuid.UUID | str
    ) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session)
            if row.status == "succeeded":
                return {**self._view(session, row), "claimed": False}
            if row.status == "running":
                return {**self._view(session, row), "claimed": False}
            if row.status in {"cancelled", "expired"}:
                raise ValueError("draft is not confirmable")
            if row.status == "pending_context":
                raise ValueError("draft is missing required context")
            row.status = "running"
            row.confirmed_at = row.confirmed_at or datetime.now(timezone.utc)
            for item in self._items(session, row.id):
                if item.status == "failed":
                    item.status = "pending"
                    item.error_code = None
            session.flush()
            return {**self._view(session, row), "claimed": True}

    def claim_item(self, import_id: uuid.UUID | str, item_id: uuid.UUID | str) -> bool:
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImportItem, uuid.UUID(str(item_id)), with_for_update=True
            )
            if row is None or row.import_id != uuid.UUID(str(import_id)):
                return False
            if row.status != "pending":
                return False
            row.status = "running"
            return True

    def finish_item(
        self,
        import_id: uuid.UUID | str,
        item_id: uuid.UUID | str,
        *,
        calendar_event_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImportItem, uuid.UUID(str(item_id)), with_for_update=True
            )
            if row is None or row.import_id != uuid.UUID(str(import_id)):
                return
            if error_code:
                row.status = "failed"
                row.error_code = str(error_code)[:128]
            else:
                row.status = "succeeded"
                row.calendar_event_id = str(calendar_event_id or "")[:256] or None
                row.error_code = None

    def finalize(self, import_id: uuid.UUID | str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if row is None:
                raise LookupError("draft not found")
            items = self._items(session, row.id)
            succeeded = sum(item.status == "succeeded" for item in items)
            failed = sum(item.status == "failed" for item in items)
            if failed:
                row.status = "partial_failed"
            elif items and succeeded == len(items):
                row.status = "succeeded"
                row.completed_at = datetime.now(timezone.utc)
            else:
                row.status = "partial_failed"
            session.flush()
            return self._view(session, row)

    def cancel(
        self, participant_id: uuid.UUID, import_id: uuid.UUID | str
    ) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session)
            if row.status == "running":
                raise ValueError("running draft cannot be cancelled")
            if row.status not in {"succeeded", "expired"}:
                row.status = "cancelled"
                row.completed_at = datetime.now(timezone.utc)
            session.flush()
            return self._view(session, row)

    @staticmethod
    def _require_owner(row: CourseScheduleImport | None, participant_id: uuid.UUID) -> None:
        if row is None:
            raise LookupError("draft not found")
        if row.participant_id != participant_id:
            raise PermissionError("draft belongs to another participant")

    @staticmethod
    def _expire_if_needed(row: CourseScheduleImport, session: Any) -> None:
        expires = _aware(row.expires_at)
        if expires <= datetime.now(timezone.utc) and row.status not in {
            "succeeded", "cancelled", "expired"
        }:
            row.status = "expired"
        if row.status == "expired":
            # Persist expiry even though the caller must reject this operation.
            session.flush()
            session.commit()
            raise ValueError("draft has expired")

    @staticmethod
    def _items(session: Any, import_id: uuid.UUID) -> list[CourseScheduleImportItem]:
        return list(
            session.execute(
                select(CourseScheduleImportItem)
                .where(CourseScheduleImportItem.import_id == import_id)
                .order_by(CourseScheduleImportItem.item_index)
            ).scalars()
        )

    def _view(self, session: Any, row: CourseScheduleImport) -> dict[str, Any]:
        items = self._items(session, row.id)
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "source_message_id": row.source_message_id,
            "source_image_hash": row.source_image_hash,
            "status": row.status,
            "semester_start_date": (
                row.semester_start_date.isoformat() if row.semester_start_date else None
            ),
            "timezone": row.timezone,
            "vision_model": row.vision_model,
            "structured_result": dict(row.structured_result or {}),
            "created_at": _aware(row.created_at).isoformat(),
            "expires_at": _aware(row.expires_at).isoformat(),
            "confirmed_at": _aware(row.confirmed_at).isoformat() if row.confirmed_at else None,
            "completed_at": _aware(row.completed_at).isoformat() if row.completed_at else None,
            "items": [
                {
                    "id": str(item.id),
                    "item_index": item.item_index,
                    "course_name": item.course_name,
                    "weekday": item.weekday,
                    "start_time": item.start_time.strftime("%H:%M") if item.start_time else None,
                    "end_time": item.end_time.strftime("%H:%M") if item.end_time else None,
                    "location": item.location,
                    "week_rule": dict(item.week_rule_json or {}),
                    "normalized_key": item.normalized_key,
                    "status": item.status,
                    "calendar_event_id": item.calendar_event_id,
                    "error_code": item.error_code,
                }
                for item in items
            ],
        }


def required_context(
    result: ScheduleVisionResult, *, semester_start_date: date | None
) -> set[str]:
    missing = set(result.missing_context)
    if semester_start_date is None:
        missing.add("semester_start_date")
    else:
        missing.discard("semester_start_date")
    for course in result.courses:
        if course.weekday is None:
            missing.add("weekday")
        if course.start_time is None or course.end_time is None:
            missing.add("period_time_mapping" if course.period_start is not None else "actual_time")
    return missing


def _parse_time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
