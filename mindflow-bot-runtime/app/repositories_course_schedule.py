"""Durable repository for participant-owned course schedule import drafts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.contracts.course_schedule import ScheduleVisionResult
from app.db import Database
from app.domain.course_schedule_periods import (
    DEFAULT_PERIOD_MAP_VERSION,
    resolve_period_time,
    split_period_mapping,
)
from app.domain.course_schedule_recurrence import (
    COURSE_IMPORT_PLANNER_VERSION,
    RECURRENCE_STRATEGIES,
)
from app.models import (
    CourseScheduleImportCompensation,
    CourseScheduleImport,
    CourseScheduleImportItem,
    CourseScheduleImportWrite,
)


ACTIVE_DRAFT_STATUSES = {"pending_context", "pending_confirmation"}
QUEUEABLE_STATUSES = {"pending_confirmation", "partial_failed"}
EXPIRABLE_STATUSES = {"pending_context", "pending_confirmation"}
DRAFT_FILLABLE_CONTEXT_FIELDS = {
    "semester_start_date",
    "period_time_mapping",
    "weekday",
    "week_rule",
    "actual_time",
}
# Kept as a compatibility alias for callers that imported the old name. These
# fields are all draft-fillable; none of them authorizes a Calendar mutation.
INTERACTIVE_CONTEXT_FIELDS = DRAFT_FILLABLE_CONTEXT_FIELDS
PROVIDER_EFFECT_STATUSES = frozenset({
    "creating",
    "created",
    "create_outcome_unknown",
    "create_identity_conflict",
})
COMPENSATION_PARENT_STATUSES = frozenset({"cancelling", "cleanup_failed"})
FINAL_PRESENTATION_STATUSES = frozenset({
    "succeeded",
    "partial_failed",
    "cancelled",
    "cleanup_failed",
})
DEFAULT_IMPORT_TIMEZONE = "Asia/Shanghai"
DEFAULT_RUN_LEASE_SECONDS = 10 * 60
RECENT_DUPLICATE_LOOKBACK_DAYS = 180
_EXPLICIT_ACTUAL_TIME_SOURCES = frozenset({"image", "user_actual"})


def _preserve_explicit_actual_time(source: str | None) -> bool:
    return source in _EXPLICIT_ACTUAL_TIME_SOURCES


class CourseCorrectionAmbiguityError(ValueError):
    """A participant-owned draft selector did not resolve to exactly one course."""

    def __init__(self, candidates: list[dict[str, Any]]):
        self.candidates = tuple(dict(value) for value in candidates[:10])
        super().__init__("correction must identify exactly one course")


class CourseScheduleImportAmbiguityError(ValueError):
    """A participant-bound import selector matched more than one import."""

    def __init__(self, candidates: list[dict[str, Any]]):
        self.candidates = tuple(dict(value) for value in candidates[:10])
        super().__init__("course schedule import selector is ambiguous")


class CourseScheduleProviderIdentityConflict(RuntimeError):
    """Two provider identities were returned for one durable source identity."""

    code = "provider_event_identity_conflict"

    def __init__(
        self,
        existing_provider_event_id: str,
        incoming_provider_event_id: str,
    ) -> None:
        self.existing_provider_event_id = existing_provider_event_id
        self.incoming_provider_event_id = incoming_provider_event_id
        super().__init__(
            "provider identity conflict: "
            f"{existing_provider_event_id} != {incoming_provider_event_id}"
        )


@dataclass(frozen=True)
class CreateDraftOutcome:
    draft: dict[str, Any]
    created_new: bool


class CourseScheduleImportRepository:
    def __init__(
        self, database: Database, *, run_lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS
    ):
        self.database = database
        self.run_lease_seconds = max(1, int(run_lease_seconds))

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
        return self.create_draft_outcome(
            participant_id,
            source_message_id=source_message_id,
            source_image_hash=source_image_hash,
            vision_model=vision_model,
            result=result,
            timezone_name=timezone_name,
            ttl_minutes=ttl_minutes,
            semester_start_date=semester_start_date,
            now=now,
        ).draft

    def create_draft_outcome(
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
    ) -> CreateDraftOutcome:
        created_at = _aware(now or datetime.now(timezone.utc))
        structured = prepare_schedule_context(result)
        missing = derive_required_context(result, semester_start_date=semester_start_date)
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
                    persisted_course = structured["courses"][index]
                    key_payload = {
                        "index": index,
                        "course": course.course_name,
                        "weekday": course.weekday,
                        "period": [course.period_start, course.period_end],
                        "time": [
                            persisted_course.get("start_time"),
                            persisted_course.get("end_time"),
                        ],
                        "location": course.location,
                        "week_rule": (
                            {
                                "start_week": course.week_rule.start_week,
                                "end_week": course.week_rule.end_week,
                                "odd_even": course.week_rule.odd_even,
                                "explicit_weeks": course.week_rule.explicit_weeks,
                            }
                            if course.week_rule is not None
                            else {}
                        ),
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
                            start_time=_parse_time(persisted_course.get("start_time")),
                            end_time=_parse_time(persisted_course.get("end_time")),
                            location=course.location,
                            week_rule_json=key_payload["week_rule"],
                            normalized_key=normalized_key,
                            status="pending",
                        )
                    )
                session.flush()
                return CreateDraftOutcome(self._view(session, row), True)
        except IntegrityError:
            existing = self.get_by_source(participant_id, source_message_id)
            if existing is None:
                raise
            return CreateDraftOutcome(existing, False)

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
            self._expire_participant_drafts(
                session, participant_id, now=now
            )
            row = session.execute(
                select(CourseScheduleImport).where(
                    CourseScheduleImport.participant_id == participant_id,
                    CourseScheduleImport.status.in_(
                        ["pending_context", "pending_confirmation"]
                    ),
                    CourseScheduleImport.recurrence_strategy.is_(None),
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
            if row.status not in ACTIVE_DRAFT_STATUSES or row.recurrence_strategy:
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
        mapping: dict[int | tuple[int, int], tuple[time, time]],
    ) -> dict[str, Any]:
        if not mapping:
            raise ValueError("period time mapping is empty")
        current_singles, current_ranges = split_period_mapping(mapping)
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session)
            if row.status not in ACTIVE_DRAFT_STATUSES or row.recurrence_strategy:
                raise ValueError("draft no longer accepts context")
            structured = dict(row.structured_result or {})
            courses = [dict(value) for value in structured.get("courses") or []]
            metadata = dict(structured.get("_metadata") or {})
            singles = _load_period_mapping(metadata.get("user_period_mapping"))
            ranges = _load_period_range_overrides(
                metadata.get("user_period_range_overrides")
            )
            singles.update(current_singles)
            ranges.update(current_ranges)
            stored_sources = metadata.get("course_time_sources")
            sources = list(stored_sources) if isinstance(stored_sources, list) else []
            sources.extend(
                "image" if course.get("start_time") and course.get("end_time") else None
                for course in courses[len(sources):]
            )
            items = self._items(session, row.id)
            for index, (course, item) in enumerate(zip(courses, items)):
                if _preserve_explicit_actual_time(sources[index]):
                    continue
                resolved = resolve_period_time(
                    course.get("period_start"),
                    course.get("period_end"),
                    period_mapping=singles,
                    range_overrides=ranges,
                )
                if resolved is None:
                    continue
                start_clock, end_clock, source = resolved
                course["start_time"] = start_clock.strftime("%H:%M")
                course["end_time"] = end_clock.strftime("%H:%M")
                item.start_time = start_clock
                item.end_time = end_clock
                sources[index] = source
            missing = set(structured.get("missing_context") or [])
            if all(course.get("start_time") and course.get("end_time") for course in courses):
                missing.discard("period_time_mapping")
                missing.discard("actual_time")
            structured["courses"] = courses
            structured["missing_context"] = sorted(missing)
            metadata["course_time_sources"] = sources
            metadata["user_period_mapping"] = {
                str(period): [start.strftime("%H:%M"), end.strftime("%H:%M")]
                for period, (start, end) in sorted(singles.items())
            }
            metadata["user_period_range_overrides"] = {
                f"{first}-{last}": [
                    start.strftime("%H:%M"), end.strftime("%H:%M")
                ]
                for (first, last), (start, end) in sorted(ranges.items())
            }
            structured["_metadata"] = metadata
            row.structured_result = structured
            row.status = "pending_context" if missing else "pending_confirmation"
            session.flush()
            return self._view(session, row)

    def apply_context_update(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        *,
        semester_start_date: date | None = None,
        period_time_mapping: dict[int | tuple[int, int], tuple[time, time]] | None = None,
    ) -> dict[str, Any]:
        """Atomically apply all missing schedule context in one transaction."""

        if semester_start_date is None and not period_time_mapping:
            raise ValueError("schedule context update is empty")
        if semester_start_date is not None and semester_start_date.weekday() != 0:
            raise ValueError("semester start date must be a Monday")
        if period_time_mapping:
            current_singles, current_ranges = split_period_mapping(period_time_mapping)
        else:
            current_singles, current_ranges = {}, {}
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session)
            if row.status not in ACTIVE_DRAFT_STATUSES or row.recurrence_strategy:
                raise ValueError("draft no longer accepts context")
            structured = dict(row.structured_result or {})
            courses = [dict(value) for value in structured.get("courses") or []]
            metadata = dict(structured.get("_metadata") or {})
            singles = _load_period_mapping(metadata.get("user_period_mapping"))
            ranges = _load_period_range_overrides(
                metadata.get("user_period_range_overrides")
            )
            singles.update(current_singles)
            ranges.update(current_ranges)
            stored_sources = metadata.get("course_time_sources")
            sources = list(stored_sources) if isinstance(stored_sources, list) else []
            sources.extend(
                "image" if course.get("start_time") and course.get("end_time") else None
                for course in courses[len(sources):]
            )
            items = self._items(session, row.id)
            for index, (course, item) in enumerate(zip(courses, items)):
                if _preserve_explicit_actual_time(sources[index]):
                    continue
                resolved = resolve_period_time(
                    course.get("period_start"),
                    course.get("period_end"),
                    period_mapping=singles,
                    range_overrides=ranges,
                )
                if resolved is None:
                    continue
                start_clock, end_clock, source = resolved
                course["start_time"] = start_clock.strftime("%H:%M")
                course["end_time"] = end_clock.strftime("%H:%M")
                item.start_time = start_clock
                item.end_time = end_clock
                sources[index] = source
            if semester_start_date is not None:
                row.semester_start_date = semester_start_date
            structured["courses"] = courses
            authoritative = ScheduleVisionResult.from_dict({
                key: structured.get(key)
                for key in (
                    "document_type", "semester_label", "institution", "courses",
                    "missing_context", "warnings",
                )
            })
            missing = derive_required_context(
                authoritative, semester_start_date=row.semester_start_date
            )
            structured["missing_context"] = sorted(missing)
            metadata["course_time_sources"] = sources
            metadata["user_period_mapping"] = {
                str(period): [start.strftime("%H:%M"), end.strftime("%H:%M")]
                for period, (start, end) in sorted(singles.items())
            }
            metadata["user_period_range_overrides"] = {
                f"{first}-{last}": [start.strftime("%H:%M"), end.strftime("%H:%M")]
                for (first, last), (start, end) in sorted(ranges.items())
            }
            structured["_metadata"] = metadata
            row.structured_result = structured
            row.status = "pending_context" if missing else "pending_confirmation"
            session.flush()
            return self._view(session, row)

    def apply_correction(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        *,
        course_name: str | None = None,
        selector_weekday: int | None = None,
        new_weekday: int | None = None,
        # Kept as a compatibility alias for callers created before the selector
        # and update weekday fields were separated.
        weekday: int | None = None,
        period_start: int | None = None,
        period_end: int | None = None,
        start_time: str | time | None = None,
        end_time: str | time | None = None,
        week_start: int | None = None,
        week_end: int | None = None,
        odd_even: str | None = None,
        explicit_weeks: list[int] | tuple[int, ...] | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Apply one structured correction to one participant-owned draft course."""

        if selector_weekday is not None and weekday is not None:
            raise ValueError("selector weekday was provided more than once")
        selector_weekday = selector_weekday if selector_weekday is not None else weekday
        for value, label in (
            (selector_weekday, "selector weekday"),
            (new_weekday, "corrected weekday"),
        ):
            if value is not None and not 1 <= value <= 7:
                raise ValueError(f"{label} is invalid")
        if (period_start is None) != (period_end is None):
            raise ValueError("corrected period range must be complete")
        if period_start is not None and not 1 <= period_start <= period_end <= 30:
            raise ValueError("corrected period range is invalid")
        if (start_time is None) != (end_time is None):
            raise ValueError("corrected actual time range must be complete")
        parsed_start = (
            start_time if isinstance(start_time, time) else _parse_time(start_time)
        )
        parsed_end = end_time if isinstance(end_time, time) else _parse_time(end_time)
        if parsed_start is not None and parsed_end <= parsed_start:
            raise ValueError("corrected actual time range is invalid")
        if (week_start is None) != (week_end is None):
            raise ValueError("corrected week range must be complete")
        if week_start is not None and not 1 <= week_start <= week_end <= 60:
            raise ValueError("corrected week range is invalid")
        if explicit_weeks is not None:
            normalized_weeks = sorted({int(value) for value in explicit_weeks})
            if not normalized_weeks or any(value < 1 or value > 60 for value in normalized_weeks):
                raise ValueError("corrected explicit weeks are invalid")
            if week_start is not None:
                raise ValueError("week range and explicit weeks are mutually exclusive")
        else:
            normalized_weeks = None
        if odd_even is not None and odd_even not in {"odd", "even", "all"}:
            raise ValueError("corrected odd/even rule is invalid")
        if not any(
            value is not None
            for value in (
                new_weekday,
                period_start,
                parsed_start,
                week_start,
                normalized_weeks,
                odd_even,
                location,
            )
        ):
            raise ValueError("correction contains no updates")
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session)
            if row.status not in ACTIVE_DRAFT_STATUSES or row.recurrence_strategy:
                raise ValueError("draft no longer accepts corrections")
            structured = dict(row.structured_result or {})
            courses = [dict(value) for value in structured.get("courses") or []]
            candidates = list(range(len(courses)))
            if course_name:
                needle = str(course_name).strip().lower()
                candidates = [
                    index
                    for index in candidates
                    if needle in str(courses[index].get("course_name") or "").lower()
                    or str(courses[index].get("course_name") or "").lower() in needle
                ]
            if selector_weekday is not None:
                candidates = [
                    index for index in candidates
                    if courses[index].get("weekday") == selector_weekday
                ]
            if len(candidates) != 1:
                raise CourseCorrectionAmbiguityError([
                    {
                        "course_name": courses[index].get("course_name"),
                        "weekday": courses[index].get("weekday"),
                    }
                    for index in candidates
                ])
            index = candidates[0]
            course = courses[index]
            items = self._items(session, row.id)
            item = items[index]
            metadata = dict(structured.get("_metadata") or {})
            sources = list(metadata.get("course_time_sources") or [])
            while len(sources) < len(courses):
                legacy_course = courses[len(sources)]
                sources.append(
                    "image"
                    if legacy_course.get("start_time")
                    and legacy_course.get("end_time")
                    else None
                )
            corrected_fields: set[str] = set()
            if new_weekday is not None:
                course["weekday"] = new_weekday
                item.weekday = new_weekday
                corrected_fields.add("weekday")
            if period_start is not None and period_end is not None:
                course["period_start"] = period_start
                course["period_end"] = period_end
                course["period_inference_source"] = "unknown"
                course["period_confidence"] = None
                preserve_explicit_time = bool(
                    _preserve_explicit_actual_time(sources[index])
                    and course.get("start_time")
                    and course.get("end_time")
                )
                if not preserve_explicit_time:
                    singles = _load_period_mapping(
                        metadata.get("user_period_mapping")
                    )
                    ranges = _load_period_range_overrides(
                        metadata.get("user_period_range_overrides")
                    )
                    resolved = resolve_period_time(
                        period_start,
                        period_end,
                        period_mapping=singles,
                        range_overrides=ranges,
                    )
                    if resolved is None:
                        course["start_time"] = None
                        course["end_time"] = None
                        item.start_time = None
                        item.end_time = None
                        sources[index] = None
                    else:
                        start_clock, end_clock, source = resolved
                        course["start_time"] = start_clock.strftime("%H:%M")
                        course["end_time"] = end_clock.strftime("%H:%M")
                        item.start_time = start_clock
                        item.end_time = end_clock
                        sources[index] = source
                corrected_fields.update({"period_start", "period_end", "actual_time"})
            if parsed_start is not None and parsed_end is not None:
                course["start_time"] = parsed_start.strftime("%H:%M")
                course["end_time"] = parsed_end.strftime("%H:%M")
                item.start_time = parsed_start
                item.end_time = parsed_end
                sources[index] = "user_actual"
                corrected_fields.update({"start_time", "end_time", "actual_time"})
            if week_start is not None and week_end is not None:
                rule = dict(course.get("week_rule") or {})
                rule.update({
                    "start_week": week_start,
                    "end_week": week_end,
                    "explicit_weeks": None,
                    "odd_even": rule.get("odd_even") or "all",
                })
                course["week_rule"] = rule
                item.week_rule_json = rule
                corrected_fields.add("week_rule")
            if normalized_weeks is not None:
                rule = dict(course.get("week_rule") or {})
                rule.update({
                    "start_week": normalized_weeks[0],
                    "end_week": normalized_weeks[-1],
                    "explicit_weeks": normalized_weeks,
                    "odd_even": rule.get("odd_even") or "all",
                })
                course["week_rule"] = rule
                item.week_rule_json = rule
                corrected_fields.add("week_rule")
            if odd_even is not None:
                rule = dict(course.get("week_rule") or {})
                if not rule:
                    raise ValueError("week range is required before odd/even correction")
                rule["odd_even"] = odd_even
                explicit_weeks = rule.get("explicit_weeks")
                if explicit_weeks is not None and odd_even in {"odd", "even"}:
                    parity = 1 if odd_even == "odd" else 0
                    filtered_weeks = sorted(
                        {
                            int(week)
                            for week in explicit_weeks
                            if int(week) % 2 == parity
                        }
                    )
                    if not filtered_weeks:
                        raise ValueError(
                            "odd/even correction removes every explicit week"
                        )
                    rule["explicit_weeks"] = filtered_weeks
                course["week_rule"] = rule
                item.week_rule_json = rule
                corrected_fields.add("week_rule")
            if location is not None:
                cleaned_location = str(location).strip()[:300]
                if not cleaned_location:
                    raise ValueError("corrected location is empty")
                course["location"] = cleaned_location
                item.location = cleaned_location
                corrected_fields.add("location")
            course["uncertain_fields"] = [
                value for value in course.get("uncertain_fields") or []
                if value not in corrected_fields
            ]
            courses[index] = course
            structured["courses"] = courses
            metadata["course_time_sources"] = sources
            corrections = list(metadata.get("user_corrections") or [])
            corrections.append({
                "course_index": index,
                "fields": sorted(corrected_fields),
            })
            metadata["user_corrections"] = corrections[-50:]
            structured["_metadata"] = metadata
            authoritative = ScheduleVisionResult.from_dict({
                key: structured.get(key)
                for key in (
                    "document_type", "semester_label", "institution", "courses",
                    "missing_context", "warnings",
                )
            })
            missing = derive_required_context(
                authoritative, semester_start_date=row.semester_start_date
            )
            structured["missing_context"] = sorted(missing)
            row.structured_result = structured
            row.status = "pending_context" if missing else "pending_confirmation"
            key_payload = {
                "index": index,
                "course": course.get("course_name"),
                "weekday": course.get("weekday"),
                "period": [course.get("period_start"), course.get("period_end")],
                "time": [course.get("start_time"), course.get("end_time")],
                "location": course.get("location"),
                "week_rule": course.get("week_rule"),
            }
            item.normalized_key = hashlib.sha256(
                json.dumps(
                    key_payload, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), default=list,
                ).encode("utf-8")
            ).hexdigest()
            session.flush()
            return self._view(session, row)

    def validate_for_confirmation(
        self, participant_id: uuid.UUID, import_id: uuid.UUID | str
    ) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc)
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session, now=checked_at)
            if self._has_identity_conflict(session, row.id):
                self._normalize_identity_conflict_row(session, row, checked_at)
            return self._view(session, row)

    def set_recurrence_strategy(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        strategy: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized = str(strategy).strip()
        if normalized not in RECURRENCE_STRATEGIES:
            raise ValueError("unsupported course recurrence strategy")
        selected_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session, now=selected_at)
            if row.status in {"pending_context", "cancelled", "expired", "succeeded"}:
                raise ValueError("draft does not accept a recurrence strategy")
            items = self._items(session, row.id)
            if row.recurrence_strategy == normalized:
                return self._view(session, row)
            if row.confirmed_at is not None or any(
                item.status != "pending" for item in items
            ):
                raise ValueError("course recurrence strategy is immutable after writes begin")
            row.recurrence_strategy = normalized
            row.recurrence_confirmed_at = selected_at
            session.flush()
            return self._view(session, row)

    def queue_import(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        *,
        recurrence_strategy: str,
        writes: list[dict[str, Any]],
        status_card_message_id: str | None = None,
        status_card_chat_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically materialize the write ledger and enqueue the durable job."""

        queued_at = _aware(now or datetime.now(timezone.utc))
        normalized_strategy = str(recurrence_strategy).strip()
        if normalized_strategy not in RECURRENCE_STRATEGIES:
            raise ValueError("unsupported course recurrence strategy")
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session, now=queued_at)
            identity_conflict = session.execute(
                select(CourseScheduleImportWrite)
                .where(
                    CourseScheduleImportWrite.import_id == row.id,
                    CourseScheduleImportWrite.status == "create_identity_conflict",
                )
                .limit(1)
            ).scalar_one_or_none()
            if identity_conflict is not None:
                self._normalize_identity_conflict_row(session, row, queued_at)
                return {
                    **self._view(session, row),
                    "queued": False,
                    "identity_conflict": True,
                }
            if row.status == "succeeded":
                return {**self._view(session, row), "queued": False}
            if row.status in {"queued", "running"}:
                if status_card_message_id:
                    row.status_card_message_id = str(status_card_message_id)[:128]
                if status_card_chat_id:
                    row.status_card_chat_id = str(status_card_chat_id)[:128]
                session.flush()
                return {**self._view(session, row), "queued": False}
            if row.status not in QUEUEABLE_STATUSES:
                raise ValueError("draft is not queueable")
            if row.recurrence_strategy not in {None, normalized_strategy}:
                raise ValueError("course recurrence strategy is immutable")
            row.recurrence_strategy = normalized_strategy
            row.recurrence_confirmed_at = row.recurrence_confirmed_at or queued_at
            row.status = "queued"
            row.confirmed_at = row.confirmed_at or queued_at
            row.run_requested_at = queued_at
            row.last_progress_at = queued_at
            row.completion_presented_at = None
            row.completion_presentation_error = None
            if status_card_message_id:
                row.status_card_message_id = str(status_card_message_id)[:128]
            if status_card_chat_id:
                row.status_card_chat_id = str(status_card_chat_id)[:128]
            existing = {
                (str(write.item_id), write.occurrence_identity): write
                for write in session.execute(
                    select(CourseScheduleImportWrite).where(
                        CourseScheduleImportWrite.import_id == row.id
                    )
                ).scalars()
            }
            for payload in writes:
                item_id = uuid.UUID(str(payload["item_id"]))
                occurrence_identity = str(payload["occurrence_identity"])[:128]
                ledger = existing.get((str(item_id), occurrence_identity))
                if ledger is None:
                    ledger = CourseScheduleImportWrite(
                        import_id=row.id,
                        item_id=item_id,
                        occurrence_identity=occurrence_identity,
                        source_identity=str(payload["source_identity"])[:512],
                        write_kind=str(payload["write_kind"])[:32],
                        summary=str(payload["summary"])[:200],
                        description=str(payload.get("description") or ""),
                        start_time=payload["start_time"],
                        end_time=payload["end_time"],
                        recurrence=(
                            str(payload["recurrence"])[:1024]
                            if payload.get("recurrence") else None
                        ),
                        affected_dates_json=[
                            value.isoformat() for value in payload["affected_dates"]
                        ],
                        status="planned",
                    )
                    session.add(ledger)
                elif ledger.status != "created":
                    ledger.status = "planned"
                    ledger.provider_event_id = None
                    ledger.error_code = None
                    ledger.updated_at = queued_at
            self._refresh_item_statuses(session, row.id)
            session.flush()
            return {**self._view(session, row), "queued": True}

    def claim_next_import(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Claim one queued job, reclaiming an expired running lease after restart."""

        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.execute(
                select(CourseScheduleImport)
                .where(
                    or_(
                        CourseScheduleImport.status == "queued",
                        (
                            (CourseScheduleImport.status == "running")
                            & (
                                CourseScheduleImport.run_claim_expires_at.is_(None)
                                | (CourseScheduleImport.run_claim_expires_at <= claimed_at)
                            )
                        ),
                    )
                )
                .order_by(CourseScheduleImport.created_at, CourseScheduleImport.id)
                .with_for_update()
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            for write in session.execute(
                select(CourseScheduleImportWrite).where(
                    CourseScheduleImportWrite.import_id == row.id,
                    CourseScheduleImportWrite.status == "creating",
                )
            ).scalars():
                # The provider may have committed before the process died.  The
                # stable source identity makes this retry safe at the provider.
                write.status = "planned"
                write.error_code = None
                write.updated_at = claimed_at
            row.status = "running"
            self._set_run_lease(row, claimed_at)
            row.last_progress_at = claimed_at
            self._refresh_item_statuses(session, row.id)
            session.flush()
            return self._view(session, row)

    def requeue_startup_recoverables(
        self, *, now: datetime | None = None
    ) -> int:
        """Requeue outcome-unknown imports once during process startup.

        This is deliberately separate from the normal polling claim query. A
        terminal ``partial_failed`` result remains user-retryable during the
        current process, while an actual process restart gets one automatic
        recovery attempt without creating a tight retry loop.
        """

        recovered_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            unknown_exists = (
                select(CourseScheduleImportWrite.id)
                .where(
                    CourseScheduleImportWrite.import_id == CourseScheduleImport.id,
                    CourseScheduleImportWrite.status == "create_outcome_unknown",
                )
                .exists()
            )
            conflict_exists = (
                select(CourseScheduleImportWrite.id)
                .where(
                    CourseScheduleImportWrite.import_id == CourseScheduleImport.id,
                    CourseScheduleImportWrite.status == "create_identity_conflict",
                )
                .exists()
            )
            rows = list(
                session.execute(
                    select(CourseScheduleImport)
                    .where(
                        CourseScheduleImport.status == "partial_failed",
                        unknown_exists,
                        ~conflict_exists,
                    )
                    .with_for_update()
                ).scalars()
            )
            for row in rows:
                row.status = "queued"
                row.completed_at = None
                row.run_requested_at = recovered_at
                row.last_progress_at = recovered_at
                self._clear_run_lease(row)
            session.flush()
            return len(rows)

    def pending_completion_presentations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return terminal imports whose final user-facing result is not durable yet."""

        with self.database.session() as session:
            rows = session.execute(
                select(CourseScheduleImport)
                .where(
                    CourseScheduleImport.status.in_(FINAL_PRESENTATION_STATUSES),
                    CourseScheduleImport.completion_presented_at.is_(None),
                )
                .order_by(CourseScheduleImport.completed_at, CourseScheduleImport.created_at)
                .limit(max(1, min(int(limit), 500)))
            ).scalars().all()
            return [self._view(session, row) for row in rows]

    def mark_completion_presented(
        self, import_id: uuid.UUID | str, *, now: datetime | None = None
    ) -> bool:
        presented_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if row is None or row.status not in FINAL_PRESENTATION_STATUSES:
                return False
            row.completion_presented_at = presented_at
            row.completion_presentation_error = None
            return True

    def mark_completion_presentation_failed(
        self,
        import_id: uuid.UUID | str,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> bool:
        failed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if row is None or row.status not in FINAL_PRESENTATION_STATUSES:
                return False
            row.completion_presented_at = None
            row.completion_presentation_error = str(error_code)[:256]
            row.last_progress_at = failed_at
            return True

    def normalize_identity_conflicts(
        self, *, now: datetime | None = None
    ) -> int:
        """Repair parent imports that predate atomic conflict aggregation."""

        normalized_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            conflict_exists = (
                select(CourseScheduleImportWrite.id)
                .where(
                    CourseScheduleImportWrite.import_id == CourseScheduleImport.id,
                    CourseScheduleImportWrite.status == "create_identity_conflict",
                )
                .exists()
            )
            rows = list(
                session.execute(
                    select(CourseScheduleImport)
                    .where(conflict_exists)
                    .with_for_update()
                ).scalars()
            )
            normalized = 0
            for row in rows:
                if self._normalize_identity_conflict_row(
                    session, row, normalized_at
                ):
                    normalized += 1
            session.flush()
            return normalized

    def claim_write(
        self,
        import_id: uuid.UUID | str,
        write_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            draft = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if draft is None or draft.status != "running":
                return None
            if draft.run_claim_expires_at is not None and _aware(
                draft.run_claim_expires_at
            ) <= claimed_at:
                return None
            row = session.get(
                CourseScheduleImportWrite, uuid.UUID(str(write_id)), with_for_update=True
            )
            if row is None or row.import_id != draft.id:
                return None
            if row.status not in {"planned", "create_outcome_unknown"}:
                return None
            row.status = "creating"
            row.error_code = None
            row.updated_at = claimed_at
            row.created_at = _aware(row.created_at)
            self._set_run_lease(draft, claimed_at)
            draft.last_progress_at = claimed_at
            session.flush()
            return self._write_view(row)

    def record_write_created(
        self,
        import_id: uuid.UUID | str,
        write_id: uuid.UUID | str,
        provider_event_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        updated_at = _aware(now or datetime.now(timezone.utc))
        normalized_provider_id = str(provider_event_id or "").strip()
        if not normalized_provider_id:
            raise ValueError("created calendar write requires provider event id")
        if len(normalized_provider_id) > 256:
            raise ValueError("calendar provider event id exceeds supported length")
        conflict: CourseScheduleProviderIdentityConflict | None = None
        with self.database.session() as session:
            draft = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            row = session.get(
                CourseScheduleImportWrite, uuid.UUID(str(write_id)), with_for_update=True
            )
            if draft is None or row is None or row.import_id != draft.id:
                return
            if row.status == "created":
                if row.provider_event_id == normalized_provider_id:
                    return
                row.status = "create_identity_conflict"
                row.provider_conflict_event_id = normalized_provider_id
                row.error_code = CourseScheduleProviderIdentityConflict.code
                row.updated_at = updated_at
                draft.completed_at = None
                draft.last_progress_at = updated_at
                if draft.status not in COMPENSATION_PARENT_STATUSES:
                    draft.status = "partial_failed"
                    self._clear_run_lease(draft)
                self._refresh_item_statuses(session, draft.id)
                conflict = CourseScheduleProviderIdentityConflict(
                    str(row.provider_event_id or ""), normalized_provider_id
                )
            elif row.status == "create_identity_conflict":
                if normalized_provider_id in {
                    row.provider_event_id,
                    row.provider_conflict_event_id,
                }:
                    return
                conflict = CourseScheduleProviderIdentityConflict(
                    str(row.provider_event_id or ""), normalized_provider_id
                )
            else:
                row.status = "created"
                row.provider_event_id = normalized_provider_id
                row.provider_conflict_event_id = None
                row.error_code = None
                row.updated_at = updated_at
                draft.last_progress_at = updated_at
                self._refresh_item_statuses(session, draft.id)
            if draft.status in COMPENSATION_PARENT_STATUSES:
                self._materialize_compensation_targets(session, draft, updated_at)
                self._finalize_cancellation_locked(session, draft, updated_at)
        if conflict is not None:
            raise conflict

    def record_write_failure(
        self,
        import_id: uuid.UUID | str,
        write_id: uuid.UUID | str,
        *,
        error_code: str,
        outcome_unknown: bool = False,
        now: datetime | None = None,
    ) -> None:
        updated_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            draft = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            row = session.get(
                CourseScheduleImportWrite, uuid.UUID(str(write_id)), with_for_update=True
            )
            if draft is None or row is None or row.import_id != draft.id:
                return
            if row.status in {"created", "create_identity_conflict"}:
                # A late failure from an older attempt cannot demote the
                # authoritative provider identity already persisted locally.
                return
            if row.status in {"create_failed", "create_outcome_unknown"}:
                # Terminal failure facts are also monotone until an explicit
                # retry claims the write and moves it back to creating.
                return
            row.status = "create_outcome_unknown" if outcome_unknown else "create_failed"
            row.error_code = str(error_code)[:128]
            row.updated_at = updated_at
            draft.last_progress_at = updated_at
            self._refresh_item_statuses(session, draft.id)
            if draft.status in COMPENSATION_PARENT_STATUSES:
                self._materialize_compensation_targets(session, draft, updated_at)
                self._finalize_cancellation_locked(session, draft, updated_at)

    def finalize_queued_import(
        self, import_id: uuid.UUID | str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        completed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if row is None:
                raise LookupError("draft not found")
            if row.status in COMPENSATION_PARENT_STATUSES:
                self._materialize_compensation_targets(session, row, completed_at)
                self._finalize_cancellation_locked(session, row, completed_at)
                session.flush()
                return self._view(session, row)
            self._refresh_item_statuses(session, row.id)
            items = self._items(session, row.id)
            writes = list(
                session.execute(
                    select(CourseScheduleImportWrite).where(
                        CourseScheduleImportWrite.import_id == row.id
                    )
                ).scalars()
            )
            if writes and all(write.status == "created" for write in writes):
                row.status = "succeeded"
                row.completed_at = completed_at
            elif not writes and items and all(item.status == "succeeded" for item in items):
                # Compatibility for imports created before the ledger migration.
                row.status = "succeeded"
                row.completed_at = completed_at
            elif any(
                write.status in {
                    "create_failed",
                    "create_outcome_unknown",
                    "create_identity_conflict",
                }
                for write in writes
            ):
                row.status = "partial_failed"
            else:
                # A runner should never finalize an incomplete job. Keep it
                # queued so a later scan can safely continue it.
                row.status = "queued"
            row.last_progress_at = completed_at
            self._clear_run_lease(row)
            session.flush()
            return self._view(session, row)

    def created_write_dates(self, import_id: uuid.UUID | str) -> set[date]:
        with self.database.session() as session:
            rows = session.execute(
                select(CourseScheduleImportWrite).where(
                    CourseScheduleImportWrite.import_id == uuid.UUID(str(import_id)),
                    CourseScheduleImportWrite.status.in_(
                        {"created", "create_identity_conflict"}
                    ),
                )
            ).scalars()
            dates: set[date] = set()
            for row in rows:
                dates.update(
                    date.fromisoformat(str(value))
                    for value in (row.affected_dates_json or [])
                )
            return dates

    def writes_for_import(
        self, import_id: uuid.UUID | str, *, statuses: set[str] | None = None
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            query = select(CourseScheduleImportWrite).where(
                CourseScheduleImportWrite.import_id == uuid.UUID(str(import_id))
            ).order_by(
                CourseScheduleImportWrite.created_at,
                CourseScheduleImportWrite.id,
            )
            if statuses:
                query = query.where(CourseScheduleImportWrite.status.in_(statuses))
            rows = list(session.execute(query).scalars())
            return [self._write_view(row) for row in rows]

    def compensations_for_import(
        self,
        import_id: uuid.UUID | str,
        *,
        statuses: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            query = select(CourseScheduleImportCompensation).where(
                CourseScheduleImportCompensation.import_id == uuid.UUID(str(import_id))
            ).order_by(
                CourseScheduleImportCompensation.created_at,
                CourseScheduleImportCompensation.id,
            )
            if statuses:
                query = query.where(CourseScheduleImportCompensation.status.in_(statuses))
            rows = list(session.execute(query).scalars())
            return [self._compensation_view(row) for row in rows]

    def request_cancel(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        *,
        mode: str | None = None,
        cancel_mode: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Install the durable cancellation fence and materialize delete targets."""

        requested_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            assert row is not None
            if (
                row.status in EXPIRABLE_STATUSES
                and _aware(row.expires_at) <= requested_at
            ):
                row.status = "expired"
                self._clear_run_lease(row)
                session.flush()
                return {**self._view(session, row), "already_expired": True}
            if row.status == "cancelled":
                return {**self._view(session, row), "already_cancelled": True}
            if row.status == "expired":
                return {**self._view(session, row), "already_expired": True}

            writes = list(
                session.execute(
                    select(CourseScheduleImportWrite).where(
                        CourseScheduleImportWrite.import_id == row.id
                    )
                ).scalars()
            )
            if mode is not None and cancel_mode is not None and mode != cancel_mode:
                raise ValueError("cancel mode was provided more than once")
            requested_mode = mode if mode is not None else cancel_mode
            if requested_mode is not None and requested_mode not in {
                "before_write", "running_cancel", "revert"
            }:
                raise ValueError("unsupported course schedule cancel mode")
            if row.status in ACTIVE_DRAFT_STATUSES and not writes:
                selected_mode = "before_write"
            elif requested_mode in {"before_write", "running_cancel", "revert"}:
                selected_mode = str(requested_mode)
            elif row.status in {"succeeded", "partial_failed"}:
                selected_mode = "revert"
            else:
                selected_mode = "running_cancel"

            if row.status in ACTIVE_DRAFT_STATUSES and not writes:
                row.status = "cancelled"
                row.cancel_mode = "before_write"
                row.cancel_requested_at = requested_at
                row.cancelled_at = requested_at
                row.completed_at = row.completed_at or requested_at
                row.cleanup_error_code = None
                self._clear_run_lease(row)
            else:
                was_cleanup_failed = row.status == "cleanup_failed"
                row.status = "cancelling"
                row.cancel_mode = selected_mode
                row.cancel_requested_at = row.cancel_requested_at or requested_at
                row.cancelled_at = None
                row.cleanup_error_code = None
                row.completion_presented_at = None
                row.completion_presentation_error = None
                # A planned row has not crossed the provider boundary and is
                # therefore safe to close without inventing a delete target.
                for write in writes:
                    if write.status == "planned":
                        write.status = "create_cancelled"
                        write.error_code = "cancelled_before_dispatch"
                        write.updated_at = requested_at
                if was_cleanup_failed:
                    for target in session.execute(
                        select(CourseScheduleImportCompensation).where(
                            CourseScheduleImportCompensation.import_id == row.id,
                            CourseScheduleImportCompensation.status == "delete_failed",
                        )
                    ).scalars():
                        target.status = "delete_pending"
                        target.error_code = None
                        target.updated_at = requested_at
                self._materialize_compensation_targets(session, row, requested_at)
                self._finalize_cancellation_locked(session, row, requested_at)
            row.last_progress_at = requested_at
            session.flush()
            result = self._view(session, row)
            result["cancel_started"] = result.get("status") in {
                "cancelling", "cleanup_failed"
            }
            result["already_cancelled"] = False
            return result

    def mark_create_cancelled(
        self,
        import_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> int:
        cancelled_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if row is None:
                return 0
            changed = 0
            for write in session.execute(
                select(CourseScheduleImportWrite).where(
                    CourseScheduleImportWrite.import_id == row.id,
                    CourseScheduleImportWrite.status == "planned",
                )
            ).scalars():
                write.status = "create_cancelled"
                write.error_code = "cancelled_before_dispatch"
                write.updated_at = cancelled_at
                changed += 1
            self._finalize_cancellation_locked(session, row, cancelled_at)
            session.flush()
            return changed

    def materialize_compensation_targets(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        materialized_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            assert row is not None
            targets = self._materialize_compensation_targets(
                session, row, materialized_at
            )
            self._finalize_cancellation_locked(session, row, materialized_at)
            session.flush()
            return [self._compensation_view(target) for target in targets]

    def claim_next_compensation(
        self, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Claim one delete target without competing with an active create attempt."""

        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            self._recover_stale_cancellation_rows(session, claimed_at)
            candidates = list(
                session.execute(
                    select(CourseScheduleImportCompensation)
                    .where(
                        CourseScheduleImportCompensation.status.in_(
                            {"delete_pending", "delete_outcome_unknown"}
                        )
                    )
                    .order_by(
                        CourseScheduleImportCompensation.created_at,
                        CourseScheduleImportCompensation.id,
                    )
                    .with_for_update()
                ).scalars()
            )
            for target in candidates:
                parent = session.get(
                    CourseScheduleImport, target.import_id, with_for_update=True
                )
                if parent is None or parent.status not in COMPENSATION_PARENT_STATUSES:
                    continue
                if self._has_active_create_attempt(session, parent, claimed_at):
                    continue
                target.delete_started_at = target.delete_started_at or claimed_at
                target.delete_claim_expires_at = claimed_at + timedelta(
                    seconds=self.run_lease_seconds
                )
                target.attempt_count = int(target.attempt_count or 0) + 1
                target.updated_at = claimed_at
                if target.status == "delete_pending":
                    target.status = "deleting"
                    target.error_code = None
                session.flush()
                return self._compensation_view(target)
            return None

    def claim_next_cancellation_reconciliation(
        self, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Claim an outcome-unknown create for same-identity replay only."""

        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            parents = list(
                session.execute(
                    select(CourseScheduleImport)
                    .where(CourseScheduleImport.status.in_(COMPENSATION_PARENT_STATUSES))
                    .order_by(CourseScheduleImport.created_at, CourseScheduleImport.id)
                    .with_for_update()
                ).scalars()
            )
            for parent in parents:
                if self._has_active_create_attempt(session, parent, claimed_at):
                    continue
                write = session.execute(
                    select(CourseScheduleImportWrite)
                    .where(
                        CourseScheduleImportWrite.import_id == parent.id,
                        CourseScheduleImportWrite.status == "create_outcome_unknown",
                    )
                    .order_by(
                        CourseScheduleImportWrite.created_at,
                        CourseScheduleImportWrite.id,
                    )
                    .with_for_update()
                    .limit(1)
                ).scalar_one_or_none()
                if write is None:
                    continue
                write.status = "creating"
                write.error_code = None
                write.updated_at = claimed_at
                self._set_run_lease(parent, claimed_at)
                parent.last_progress_at = claimed_at
                session.flush()
                return {
                    **self._write_view(write),
                    "participant_id": str(parent.participant_id),
                    "import_status": parent.status,
                }
            return None

    def claim_compensation_target(
        self,
        target_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            target = session.get(
                CourseScheduleImportCompensation,
                uuid.UUID(str(target_id)),
                with_for_update=True,
            )
            if target is None or target.status not in {
                "delete_pending", "delete_outcome_unknown"
            }:
                return None
            parent = session.get(
                CourseScheduleImport, target.import_id, with_for_update=True
            )
            if parent is None or parent.status not in COMPENSATION_PARENT_STATUSES:
                return None
            if self._has_active_create_attempt(session, parent, claimed_at):
                return None
            target.delete_started_at = target.delete_started_at or claimed_at
            target.delete_claim_expires_at = claimed_at + timedelta(
                seconds=self.run_lease_seconds
            )
            target.attempt_count = int(target.attempt_count or 0) + 1
            target.updated_at = claimed_at
            if target.status == "delete_pending":
                target.status = "deleting"
                target.error_code = None
            session.flush()
            return self._compensation_view(target)

    def record_delete_success(
        self,
        target_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        deleted_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            target = session.get(
                CourseScheduleImportCompensation,
                uuid.UUID(str(target_id)),
                with_for_update=True,
            )
            if target is None:
                return None
            target.status = "deleted"
            target.error_code = None
            target.deleted_at = target.deleted_at or deleted_at
            target.updated_at = deleted_at
            target.delete_claim_expires_at = None
            # The provider fact and the downstream rollback obligation must
            # commit together. A duplicate delete-success notification may
            # refresh an incomplete obligation, but never re-open completed
            # downstream work.
            if target.rollback_refresh_status != "completed":
                target.rollback_refresh_status = "pending"
                target.rollback_refresh_next_attempt_at = deleted_at
                target.rollback_refresh_claim_expires_at = None
                target.rollback_refresh_completed_at = None
                target.rollback_refresh_error_code = None
            parent = session.get(
                CourseScheduleImport, target.import_id, with_for_update=True
            )
            if parent is not None:
                self._finalize_cancellation_locked(session, parent, deleted_at)
            session.flush()
            result = self._compensation_view(target)
            result["finalized"] = bool(parent and parent.status == "cancelled")
            return result

    def record_delete_failure(
        self,
        target_id: uuid.UUID | str,
        *,
        error_code: str,
        outcome_unknown: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        failed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            target = session.get(
                CourseScheduleImportCompensation,
                uuid.UUID(str(target_id)),
                with_for_update=True,
            )
            if target is None:
                return None
            if target.status == "deleted":
                return self._compensation_view(target)
            target.status = "delete_outcome_unknown" if outcome_unknown else "delete_failed"
            target.error_code = str(error_code)[:128]
            target.updated_at = failed_at
            target.delete_claim_expires_at = None
            parent = session.get(
                CourseScheduleImport, target.import_id, with_for_update=True
            )
            if parent is not None:
                self._finalize_cancellation_locked(session, parent, failed_at)
            session.flush()
            return self._compensation_view(target)

    def claim_next_rollback_refresh(
        self, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Claim one durable Forecast/Warning rollback obligation."""

        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            rows = list(
                session.execute(
                    select(CourseScheduleImportCompensation)
                    .where(
                        CourseScheduleImportCompensation.status == "deleted",
                        CourseScheduleImportCompensation.rollback_refresh_status.in_(
                            {"pending", "processing"}
                        ),
                        or_(
                            CourseScheduleImportCompensation.rollback_refresh_next_attempt_at.is_(None),
                            CourseScheduleImportCompensation.rollback_refresh_next_attempt_at
                            <= claimed_at,
                        ),
                    )
                    .order_by(
                        CourseScheduleImportCompensation.rollback_refresh_next_attempt_at,
                        CourseScheduleImportCompensation.updated_at,
                        CourseScheduleImportCompensation.id,
                    )
                    .with_for_update()
                ).scalars()
            )
            for target in rows:
                if target.rollback_refresh_status == "processing":
                    lease_expires = target.rollback_refresh_claim_expires_at
                    if lease_expires is not None and _aware(lease_expires) > claimed_at:
                        continue
                target.rollback_refresh_status = "processing"
                target.rollback_refresh_attempt_count = int(
                    target.rollback_refresh_attempt_count or 0
                ) + 1
                target.rollback_refresh_claim_expires_at = claimed_at + timedelta(
                    seconds=self.run_lease_seconds
                )
                target.updated_at = claimed_at
                session.flush()
                return self._compensation_view(target)
            return None

    def mark_rollback_refresh_completed(
        self,
        target_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        completed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            target = session.get(
                CourseScheduleImportCompensation,
                uuid.UUID(str(target_id)),
                with_for_update=True,
            )
            if target is None or target.status != "deleted":
                return None
            target.rollback_refresh_status = "completed"
            target.rollback_refresh_claim_expires_at = None
            target.rollback_refresh_next_attempt_at = None
            target.rollback_refresh_completed_at = (
                target.rollback_refresh_completed_at or completed_at
            )
            target.rollback_refresh_error_code = None
            target.updated_at = completed_at
            session.flush()
            return self._compensation_view(target)

    def mark_rollback_refresh_retry(
        self,
        target_id: uuid.UUID | str,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        failed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            target = session.get(
                CourseScheduleImportCompensation,
                uuid.UUID(str(target_id)),
                with_for_update=True,
            )
            if target is None or target.status != "deleted":
                return None
            attempt = max(1, int(target.rollback_refresh_attempt_count or 0))
            delay_seconds = (30, 120, 600, 1800)[min(attempt - 1, 3)]
            target.rollback_refresh_status = "pending"
            target.rollback_refresh_next_attempt_at = failed_at + timedelta(
                seconds=delay_seconds
            )
            target.rollback_refresh_claim_expires_at = None
            target.rollback_refresh_error_code = str(error_code)[:128]
            target.updated_at = failed_at
            session.flush()
            return self._compensation_view(target)

    def finalize_cancellation(
        self,
        import_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        finalized_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if row is None:
                raise LookupError("draft not found")
            self._materialize_compensation_targets(session, row, finalized_at)
            self._finalize_cancellation_locked(session, row, finalized_at)
            session.flush()
            return self._view(session, row)

    def recover_stale_cancellation_work(
        self, *, now: datetime | None = None
    ) -> int:
        recovered_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            count = self._recover_stale_cancellation_rows(session, recovered_at)
            rows = list(
                session.execute(
                    select(CourseScheduleImport)
                    .where(CourseScheduleImport.status.in_(COMPENSATION_PARENT_STATUSES))
                    .with_for_update()
                ).scalars()
            )
            for row in rows:
                self._materialize_compensation_targets(session, row, recovered_at)
                self._finalize_cancellation_locked(session, row, recovered_at)
            session.flush()
            return count

    def resume_cleanup_for_participant(
        self,
        participant_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> int:
        resumed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            rows = list(
                session.execute(
                    select(CourseScheduleImport)
                    .where(
                        CourseScheduleImport.participant_id == participant_id,
                        CourseScheduleImport.status == "cleanup_failed",
                    )
                    .with_for_update()
                ).scalars()
            )
            for row in rows:
                row.status = "cancelling"
                row.cleanup_error_code = None
                # A previously presented cleanup_failed card is not the final
                # cancelled result. Re-open durable presentation recovery.
                row.completion_presented_at = None
                row.completion_presentation_error = None
                row.last_progress_at = resumed_at
                for target in session.execute(
                    select(CourseScheduleImportCompensation).where(
                        CourseScheduleImportCompensation.import_id == row.id,
                        CourseScheduleImportCompensation.status == "delete_failed",
                    )
                ).scalars():
                    target.status = "delete_pending"
                    target.error_code = None
                    target.updated_at = resumed_at
                self._materialize_compensation_targets(session, row, resumed_at)
            session.flush()
            return len(rows)

    # Compatibility spelling for the OAuth completion callback.
    resume_cleanup = resume_cleanup_for_participant

    def recent_cancel_candidates(
        self, participant_id: uuid.UUID, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        statuses = {
            "pending_context", "pending_confirmation", "queued", "running",
            "cancelling", "succeeded", "partial_failed", "cleanup_failed",
            "cancelled",
        }
        with self.database.session() as session:
            self._expire_participant_drafts(
                session, participant_id, now=datetime.now(timezone.utc)
            )
            rows = list(
                session.execute(
                    select(CourseScheduleImport)
                    .where(
                        CourseScheduleImport.participant_id == participant_id,
                        CourseScheduleImport.status.in_(statuses),
                    )
                    .order_by(
                        CourseScheduleImport.created_at.desc(),
                        CourseScheduleImport.id.desc(),
                    )
                    .limit(max(1, min(int(limit), 50)))
                ).scalars()
            )
            output = []
            for row in rows:
                writes = list(
                    session.execute(
                        select(CourseScheduleImportWrite).where(
                            CourseScheduleImportWrite.import_id == row.id
                        )
                    ).scalars()
                )
                courses = list((row.structured_result or {}).get("courses") or [])
                names = [str(course.get("course_name") or "") for course in courses]
                if not names:
                    names = [
                        str(item.course_name)
                        for item in self._items(session, row.id)
                    ]
                try:
                    import_timezone = ZoneInfo(
                        str(row.timezone or DEFAULT_IMPORT_TIMEZONE)
                    )
                except Exception:
                    import_timezone = ZoneInfo(DEFAULT_IMPORT_TIMEZONE)
                created_local_date = (
                    _aware(row.created_at)
                    .astimezone(import_timezone)
                    .date()
                    .isoformat()
                )
                output.append({
                    "id": str(row.id),
                    "participant_id": str(row.participant_id),
                    "status": row.status,
                    "created_at": _aware(row.created_at).isoformat(),
                    "created_local_date": created_local_date,
                    "source_image_hash": row.source_image_hash,
                    "course_names": [name[:80] for name in names[:10]],
                    "has_provider_effect": any(
                        write.status in PROVIDER_EFFECT_STATUSES
                        or bool(write.provider_event_id)
                        or bool(write.provider_conflict_event_id)
                        for write in writes
                    ),
                })
            return output

    @staticmethod
    def _expire_participant_drafts(
        session: Any,
        participant_id: uuid.UUID,
        *,
        now: datetime,
    ) -> int:
        """Archive stale active drafts before exposing participant-facing views."""

        rows = list(
            session.execute(
                select(CourseScheduleImport)
                .where(
                    CourseScheduleImport.participant_id == participant_id,
                    CourseScheduleImport.status.in_(EXPIRABLE_STATUSES),
                    CourseScheduleImport.expires_at <= _aware(now),
                )
                .with_for_update()
            ).scalars()
        )
        for row in rows:
            row.status = "expired"
            CourseScheduleImportRepository._clear_run_lease(row)
        if rows:
            session.flush()
        return len(rows)

    def resolve_cancel_selector(
        self, participant_id: uuid.UUID, selector: dict[str, Any]
    ) -> dict[str, Any]:
        selector = dict(selector or {})
        candidates = self.recent_cancel_candidates(participant_id, limit=50)
        if selector.get("latest") is True:
            matches = candidates[:1]
        elif selector.get("course_name"):
            needle = str(selector["course_name"]).strip().lower()
            matches = [
                candidate for candidate in candidates
                if any(
                    needle in name.lower() or name.lower() in needle
                    for name in candidate.get("course_names") or []
                )
            ]
        elif selector.get("created_date"):
            value = str(selector["created_date"])
            matches = [
                candidate for candidate in candidates
                if str(candidate.get("created_local_date") or "") == value
            ]
        else:
            raise ValueError("cancel selector is required")
        if not matches:
            raise LookupError("course schedule import not found")
        if len(matches) != 1:
            raise CourseScheduleImportAmbiguityError(matches)
        return matches[0]

    def begin_confirmation(
        self,
        participant_id: uuid.UUID,
        import_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session, now=claimed_at)
            if row.status in COMPENSATION_PARENT_STATUSES:
                # Cancellation owns the import until every provider effect is
                # compensated; a late confirmation must not reopen creation.
                return {**self._view(session, row), "claimed": False}
            if row.status == "succeeded":
                return {**self._view(session, row), "claimed": False}
            if row.status == "running":
                lease_expires = (
                    _aware(row.run_claim_expires_at)
                    if row.run_claim_expires_at is not None
                    else None
                )
                if lease_expires is not None and lease_expires > claimed_at:
                    return {**self._view(session, row), "claimed": False}
                for item in self._items(session, row.id):
                    if item.status == "running":
                        item.status = "pending"
                        item.error_code = None
            if row.status in {"cancelled", "expired"}:
                raise ValueError("draft is not confirmable")
            if row.status == "pending_context":
                raise ValueError("draft is missing required context")
            if row.recurrence_strategy not in RECURRENCE_STRATEGIES:
                raise ValueError("draft has no confirmed recurrence strategy")
            row.status = "running"
            row.confirmed_at = row.confirmed_at or claimed_at
            self._set_run_lease(row, claimed_at)
            for item in self._items(session, row.id):
                if item.status == "failed":
                    item.status = "pending"
                    item.error_code = None
            session.flush()
            return {**self._view(session, row), "claimed": True}

    def claim_item(
        self,
        import_id: uuid.UUID | str,
        item_id: uuid.UUID | str,
        *,
        now: datetime | None = None,
    ) -> bool:
        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            draft = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if draft is None or draft.status != "running":
                return False
            row = session.get(
                CourseScheduleImportItem, uuid.UUID(str(item_id)), with_for_update=True
            )
            if row is None or row.import_id != uuid.UUID(str(import_id)):
                return False
            if row.status != "pending":
                return False
            row.status = "running"
            self._set_run_lease(draft, claimed_at)
            return True

    def renew_run_lease(
        self, import_id: uuid.UUID | str, *, now: datetime | None = None
    ) -> bool:
        claimed_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            if row is None or row.status != "running":
                return False
            self._set_run_lease(row, claimed_at)
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
            self._clear_run_lease(row)
            session.flush()
            return self._view(session, row)

    def cancel(
        self, participant_id: uuid.UUID, import_id: uuid.UUID | str
    ) -> dict[str, Any]:
        return self.request_cancel(participant_id, import_id)

    def _materialize_compensation_targets(
        self,
        session: Any,
        row: CourseScheduleImport,
        materialized_at: datetime,
    ) -> list[CourseScheduleImportCompensation]:
        existing = {
            (target.write_id, target.provider_event_id): target
            for target in session.execute(
                select(CourseScheduleImportCompensation).where(
                    CourseScheduleImportCompensation.import_id == row.id
                )
            ).scalars()
        }
        writes = list(
            session.execute(
                select(CourseScheduleImportWrite).where(
                    CourseScheduleImportWrite.import_id == row.id,
                    CourseScheduleImportWrite.status.in_(
                        {"created", "create_identity_conflict"}
                    ),
                )
            ).scalars()
        )
        result = list(existing.values())
        for write in writes:
            identities = [
                ("primary", write.provider_event_id),
                ("conflict", write.provider_conflict_event_id),
            ]
            for identity_kind, provider_id in identities:
                normalized = str(provider_id or "").strip()
                if not normalized:
                    continue
                key = (write.id, normalized)
                if key in existing:
                    continue
                target = CourseScheduleImportCompensation(
                    import_id=row.id,
                    write_id=write.id,
                    participant_id=row.participant_id,
                    provider_event_id=normalized,
                    provider_identity_kind=identity_kind,
                    affected_dates_json=list(write.affected_dates_json or []),
                    status="delete_pending",
                    attempt_count=0,
                    created_at=materialized_at,
                    updated_at=materialized_at,
                )
                session.add(target)
                session.flush()
                existing[key] = target
                result.append(target)
        return result

    def _recover_stale_cancellation_rows(
        self, session: Any, recovered_at: datetime
    ) -> int:
        recovered = 0
        parents = list(
            session.execute(
                select(CourseScheduleImport)
                .where(CourseScheduleImport.status.in_(COMPENSATION_PARENT_STATUSES))
                .with_for_update()
            ).scalars()
        )
        for parent in parents:
            run_expired = (
                parent.run_claim_expires_at is None
                or _aware(parent.run_claim_expires_at) <= recovered_at
            )
            if run_expired:
                for write in session.execute(
                    select(CourseScheduleImportWrite).where(
                        CourseScheduleImportWrite.import_id == parent.id,
                        CourseScheduleImportWrite.status == "creating",
                    )
                ).scalars():
                    write.status = "create_outcome_unknown"
                    write.error_code = "course_schedule_create_interrupted"
                    write.updated_at = recovered_at
                    recovered += 1
                self._clear_run_lease(parent)
            for target in session.execute(
                select(CourseScheduleImportCompensation).where(
                    CourseScheduleImportCompensation.import_id == parent.id,
                    CourseScheduleImportCompensation.status == "deleting",
                )
            ).scalars():
                deadline = target.delete_claim_expires_at
                if deadline is None or _aware(deadline) <= recovered_at:
                    target.status = "delete_outcome_unknown"
                    target.error_code = "course_schedule_delete_interrupted"
                    target.updated_at = recovered_at
                    target.delete_claim_expires_at = None
                    recovered += 1
        return recovered

    def _has_active_create_attempt(
        self,
        session: Any,
        parent: CourseScheduleImport,
        at: datetime,
    ) -> bool:
        creating = list(
            session.execute(
                select(CourseScheduleImportWrite).where(
                    CourseScheduleImportWrite.import_id == parent.id,
                    CourseScheduleImportWrite.status == "creating",
                )
            ).scalars()
        )
        for write in creating:
            if parent.run_claim_expires_at is not None and _aware(
                parent.run_claim_expires_at
            ) > at:
                return True
            write.status = "create_outcome_unknown"
            write.error_code = "course_schedule_create_interrupted"
            write.updated_at = at
        return False

    def _finalize_cancellation_locked(
        self,
        session: Any,
        row: CourseScheduleImport,
        finalized_at: datetime,
    ) -> bool:
        if row.status not in COMPENSATION_PARENT_STATUSES:
            return row.status == "cancelled"
        writes = list(
            session.execute(
                select(CourseScheduleImportWrite).where(
                    CourseScheduleImportWrite.import_id == row.id
                )
            ).scalars()
        )
        targets = list(
            session.execute(
                select(CourseScheduleImportCompensation).where(
                    CourseScheduleImportCompensation.import_id == row.id
                )
            ).scalars()
        )
        identities = {
            (write.id, str(provider_id).strip())
            for write in writes
            for provider_id in (
                write.provider_event_id,
                write.provider_conflict_event_id,
            )
            if write.status in {"created", "create_identity_conflict"}
            and str(provider_id or "").strip()
        }
        target_keys = {(target.write_id, target.provider_event_id) for target in targets}
        create_pending = any(
            write.status in {"planned", "creating", "create_outcome_unknown"}
            for write in writes
        )
        if any(target.status == "delete_failed" for target in targets):
            if row.status != "cleanup_failed":
                row.completion_presented_at = None
                row.completion_presentation_error = None
            row.status = "cleanup_failed"
            row.cleanup_error_code = next(
                (target.error_code for target in targets if target.status == "delete_failed"),
                "calendar_delete_failed",
            )
            row.last_progress_at = finalized_at
            return False
        clean = (
            not create_pending
            and identities.issubset(target_keys)
            and all(target.status == "deleted" for target in targets)
        )
        if clean:
            row.status = "cancelled"
            row.cancelled_at = row.cancelled_at or finalized_at
            row.completed_at = row.completed_at or finalized_at
            row.cleanup_error_code = None
            self._clear_run_lease(row)
            row.last_progress_at = finalized_at
            return True
        row.status = "cancelling"
        row.last_progress_at = finalized_at
        return False

    @staticmethod
    def _require_owner(row: CourseScheduleImport | None, participant_id: uuid.UUID) -> None:
        if row is None:
            raise LookupError("draft not found")
        if row.participant_id != participant_id:
            raise PermissionError("draft belongs to another participant")

    @staticmethod
    def _expire_if_needed(
        row: CourseScheduleImport, session: Any, *, now: datetime | None = None
    ) -> None:
        expires = _aware(row.expires_at)
        if (
            expires <= _aware(now or datetime.now(timezone.utc))
            and row.status in EXPIRABLE_STATUSES
        ):
            row.status = "expired"
            CourseScheduleImportRepository._clear_run_lease(row)
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

    @staticmethod
    def _has_identity_conflict(session: Any, import_id: uuid.UUID) -> bool:
        return (
            session.execute(
                select(CourseScheduleImportWrite.id)
                .where(
                    CourseScheduleImportWrite.import_id == import_id,
                    CourseScheduleImportWrite.status == "create_identity_conflict",
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )

    @staticmethod
    def _normalize_identity_conflict_row(
        session: Any,
        row: CourseScheduleImport,
        normalized_at: datetime,
    ) -> bool:
        cancellation_active = row.status in COMPENSATION_PARENT_STATUSES
        changed = bool(
            (not cancellation_active and row.status != "partial_failed")
            or row.completed_at is not None
            or (not cancellation_active and row.run_claimed_at is not None)
            or (not cancellation_active and row.run_claim_expires_at is not None)
        )
        if not cancellation_active:
            row.status = "partial_failed"
        row.completed_at = None
        row.completion_presented_at = None
        row.completion_presentation_error = None
        row.last_progress_at = normalized_at
        if not cancellation_active:
            CourseScheduleImportRepository._clear_run_lease(row)
        CourseScheduleImportRepository._refresh_item_statuses(session, row.id)
        return changed

    @staticmethod
    def _refresh_item_statuses(session: Any, import_id: uuid.UUID) -> None:
        items = CourseScheduleImportRepository._items(session, import_id)
        writes = list(
            session.execute(
                select(CourseScheduleImportWrite).where(
                    CourseScheduleImportWrite.import_id == import_id
                )
            ).scalars()
        )
        by_item: dict[uuid.UUID, list[CourseScheduleImportWrite]] = {}
        for write in writes:
            by_item.setdefault(write.item_id, []).append(write)
        for item in items:
            item_writes = by_item.get(item.id, [])
            if not item_writes:
                # Keep legacy item state intact until a future migration can
                # backfill historical imports into the write ledger.
                continue
            statuses = {write.status for write in item_writes}
            if item_writes and statuses == {"created"}:
                item.status = "succeeded"
                item.error_code = None
                provider_ids = [
                    str(write.provider_event_id)
                    for write in item_writes
                    if write.provider_event_id
                ]
                if provider_ids:
                    # Compatibility-only summary; the ledger is authoritative.
                    item.calendar_event_id = provider_ids[-1][:256]
            elif "create_identity_conflict" in statuses:
                item.status = "failed"
                item.error_code = "provider_event_identity_conflict"
            elif statuses & {"creating", "create_outcome_unknown"}:
                item.status = "running"
                unknown = next(
                    (
                        write.error_code
                        for write in item_writes
                        if write.status == "create_outcome_unknown"
                    ),
                    None,
                )
                item.error_code = unknown
            elif "create_failed" in statuses:
                item.status = "failed"
                item.error_code = next(
                    (
                        write.error_code
                        for write in item_writes
                        if write.status == "create_failed"
                    ),
                    "calendar_write_failed",
                )
            else:
                item.status = "pending"
                item.error_code = None

    @staticmethod
    def _write_view(row: CourseScheduleImportWrite) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "import_id": str(row.import_id),
            "item_id": str(row.item_id),
            "occurrence_identity": row.occurrence_identity,
            "source_identity": row.source_identity,
            "write_kind": row.write_kind,
            "summary": row.summary,
            "description": row.description,
            "start_time": _aware(row.start_time).isoformat(),
            "end_time": _aware(row.end_time).isoformat(),
            "recurrence": row.recurrence,
            "affected_dates": [str(value) for value in (row.affected_dates_json or [])],
            "status": row.status,
            "provider_event_id": row.provider_event_id,
            "provider_conflict_event_id": row.provider_conflict_event_id,
            "error_code": row.error_code,
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
        }

    @staticmethod
    def _compensation_view(row: CourseScheduleImportCompensation) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "import_id": str(row.import_id),
            "write_id": str(row.write_id),
            "participant_id": str(row.participant_id),
            "provider_event_id": row.provider_event_id,
            "provider_identity_kind": row.provider_identity_kind,
            "affected_dates": [
                str(value) for value in (row.affected_dates_json or [])
            ],
            "status": row.status,
            "error_code": row.error_code,
            "attempt_count": int(row.attempt_count or 0),
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "delete_started_at": (
                _aware(row.delete_started_at).isoformat()
                if row.delete_started_at else None
            ),
            "deleted_at": (
                _aware(row.deleted_at).isoformat() if row.deleted_at else None
            ),
            "delete_claim_expires_at": (
                _aware(row.delete_claim_expires_at).isoformat()
                if row.delete_claim_expires_at else None
            ),
            "rollback_refresh_status": row.rollback_refresh_status,
            "rollback_refresh_attempt_count": int(
                row.rollback_refresh_attempt_count or 0
            ),
            "rollback_refresh_next_attempt_at": (
                _aware(row.rollback_refresh_next_attempt_at).isoformat()
                if row.rollback_refresh_next_attempt_at else None
            ),
            "rollback_refresh_claim_expires_at": (
                _aware(row.rollback_refresh_claim_expires_at).isoformat()
                if row.rollback_refresh_claim_expires_at else None
            ),
            "rollback_refresh_completed_at": (
                _aware(row.rollback_refresh_completed_at).isoformat()
                if row.rollback_refresh_completed_at else None
            ),
            "rollback_refresh_error_code": row.rollback_refresh_error_code,
        }

    def _view(self, session: Any, row: CourseScheduleImport) -> dict[str, Any]:
        items = self._items(session, row.id)
        duplicate_warning = self._recent_duplicate_warning(session, row, items)
        writes = list(
            session.execute(
                select(CourseScheduleImportWrite).where(
                    CourseScheduleImportWrite.import_id == row.id
                ).order_by(
                    CourseScheduleImportWrite.created_at,
                    CourseScheduleImportWrite.id,
                )
            ).scalars()
        )
        result = {
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
            "recurrence_strategy": row.recurrence_strategy,
            "recurrence_confirmed_at": (
                _aware(row.recurrence_confirmed_at).isoformat()
                if row.recurrence_confirmed_at else None
            ),
            "created_at": _aware(row.created_at).isoformat(),
            "expires_at": _aware(row.expires_at).isoformat(),
            "confirmed_at": _aware(row.confirmed_at).isoformat() if row.confirmed_at else None,
            "run_requested_at": (
                _aware(row.run_requested_at).isoformat() if row.run_requested_at else None
            ),
            "status_card_message_id": row.status_card_message_id,
            "status_card_chat_id": row.status_card_chat_id,
            "last_progress_at": (
                _aware(row.last_progress_at).isoformat() if row.last_progress_at else None
            ),
            "completion_presented_at": (
                _aware(row.completion_presented_at).isoformat()
                if row.completion_presented_at else None
            ),
            "completion_presentation_error": row.completion_presentation_error,
            "cancel_requested_at": (
                _aware(row.cancel_requested_at).isoformat()
                if row.cancel_requested_at else None
            ),
            "cancel_mode": row.cancel_mode,
            "cancelled_at": (
                _aware(row.cancelled_at).isoformat() if row.cancelled_at else None
            ),
            "cleanup_error_code": row.cleanup_error_code,
            "run_claimed_at": (
                _aware(row.run_claimed_at).isoformat() if row.run_claimed_at else None
            ),
            "run_claim_expires_at": (
                _aware(row.run_claim_expires_at).isoformat()
                if row.run_claim_expires_at else None
            ),
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
            "writes": [self._write_view(write) for write in writes],
            "compensations": [
                self._compensation_view(compensation)
                for compensation in session.execute(
                    select(CourseScheduleImportCompensation).where(
                        CourseScheduleImportCompensation.import_id == row.id
                    ).order_by(
                        CourseScheduleImportCompensation.created_at,
                        CourseScheduleImportCompensation.id,
                    )
                ).scalars()
            ],
        }
        if duplicate_warning is not None:
            result["duplicate_warning"] = duplicate_warning
        return result

    @staticmethod
    def _recent_duplicate_warning(
        session: Any,
        row: CourseScheduleImport,
        items: list[CourseScheduleImportItem],
    ) -> dict[str, Any] | None:
        if row.status not in ACTIVE_DRAFT_STATUSES or not items:
            return None
        normalized_keys = {item.normalized_key for item in items}
        cutoff = _aware(row.created_at) - timedelta(
            days=RECENT_DUPLICATE_LOOKBACK_DAYS
        )
        matches = list(
            session.execute(
                select(
                    CourseScheduleImportItem.normalized_key,
                    CourseScheduleImportItem.course_name,
                )
                .join(
                    CourseScheduleImport,
                    CourseScheduleImport.id == CourseScheduleImportItem.import_id,
                )
                .where(
                    CourseScheduleImport.participant_id == row.participant_id,
                    CourseScheduleImport.id != row.id,
                    CourseScheduleImport.status.in_(
                        {"succeeded", "partial_failed", "cancelling", "cleanup_failed"}
                    ),
                    CourseScheduleImport.created_at >= cutoff,
                    CourseScheduleImportItem.status == "succeeded",
                    CourseScheduleImportItem.normalized_key.in_(normalized_keys),
                )
            ).all()
        )
        if not matches:
            return None
        names_by_key = {str(key): str(name) for key, name in matches}
        ordered_names = [
            item.course_name
            for item in items
            if item.normalized_key in names_by_key
        ]
        return {
            "count": len(ordered_names),
            "course_names": ordered_names[:20],
            "lookback_days": RECENT_DUPLICATE_LOOKBACK_DAYS,
        }

    def _set_run_lease(self, row: CourseScheduleImport, claimed_at: datetime) -> None:
        row.run_claimed_at = claimed_at
        row.run_claim_expires_at = claimed_at + timedelta(
            seconds=self.run_lease_seconds
        )

    @staticmethod
    def _clear_run_lease(row: CourseScheduleImport) -> None:
        row.run_claimed_at = None
        row.run_claim_expires_at = None


def derive_required_context(
    result: ScheduleVisionResult, *, semester_start_date: date | None = None
) -> set[str]:
    missing: set[str] = set()
    if semester_start_date is None:
        missing.add("semester_start_date")
    else:
        missing.discard("semester_start_date")
    for course in result.courses:
        if course.weekday is None:
            missing.add("weekday")
        if course.week_rule is None:
            missing.add("week_rule")
        if course.start_time is None or course.end_time is None:
            if course.period_start is None:
                missing.add("actual_time")
            elif resolve_period_time(course.period_start, course.period_end) is None:
                missing.add("period_time_mapping")
    return missing


def _parse_time(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_period_mapping(value: Any) -> dict[int, tuple[time, time]]:
    if not isinstance(value, dict):
        return {}
    result: dict[int, tuple[time, time]] = {}
    for raw_period, raw_clocks in value.items():
        try:
            period = int(raw_period)
            start, end = _metadata_clocks(raw_clocks)
        except (TypeError, ValueError):
            continue
        if 1 <= period <= 30:
            result[period] = (start, end)
    return result


def _load_period_range_overrides(
    value: Any,
) -> dict[tuple[int, int], tuple[time, time]]:
    if not isinstance(value, dict):
        return {}
    result: dict[tuple[int, int], tuple[time, time]] = {}
    for raw_range, raw_clocks in value.items():
        try:
            first_text, last_text = str(raw_range).split("-", 1)
            first, last = int(first_text), int(last_text)
            start, end = _metadata_clocks(raw_clocks)
        except (TypeError, ValueError):
            continue
        if 1 <= first <= last <= 30:
            result[(first, last)] = (start, end)
    return result


def _metadata_clocks(value: Any) -> tuple[time, time]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("persisted period mapping is invalid")
    start, end = time.fromisoformat(str(value[0])), time.fromisoformat(str(value[1]))
    if end <= start:
        raise ValueError("persisted period mapping range is invalid")
    return start, end


def prepare_schedule_context(result: ScheduleVisionResult) -> dict[str, Any]:
    """Resolve trusted, read-only schedule facts using backend period rules."""

    structured = result.to_dict()
    parse_report = structured.pop("_parse_report", None)
    sources: list[str | None] = []
    for raw, course in zip(structured["courses"], result.courses):
        resolved = resolve_period_time(
            course.period_start,
            course.period_end,
            actual_start=_parse_time(course.start_time),
            actual_end=_parse_time(course.end_time),
        )
        if resolved is None:
            sources.append(None)
            continue
        start_clock, end_clock, source = resolved
        raw["start_time"] = start_clock.strftime("%H:%M")
        raw["end_time"] = end_clock.strftime("%H:%M")
        sources.append(source)
    if structured["courses"]:
        missing = set(structured.get("missing_context") or [])
        if all(course.get("weekday") is not None for course in structured["courses"]):
            missing.discard("weekday")
        if all(course.get("week_rule") is not None for course in structured["courses"]):
            missing.discard("week_rule")
        if all(
            course.get("start_time") and course.get("end_time")
            for course in structured["courses"]
        ):
            missing.discard("actual_time")
            missing.discard("period_time_mapping")
        structured["missing_context"] = sorted(missing)
    structured["_metadata"] = {
        "planner_version": COURSE_IMPORT_PLANNER_VERSION,
        "period_map_version": DEFAULT_PERIOD_MAP_VERSION,
        "course_time_sources": sources,
        "period_inference": [
            {
                "source": course.period_inference_source or "unknown",
                "confidence": course.period_confidence,
            }
            for course in result.courses
        ],
    }
    if parse_report:
        structured["_metadata"]["parse_report"] = parse_report
    return structured
