"""Durable repository for participant-owned course schedule import drafts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any
import uuid

from sqlalchemy import select
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
from app.models import CourseScheduleImport, CourseScheduleImportItem


ACTIVE_DRAFT_STATUSES = {"pending_context", "pending_confirmation"}
EXPIRABLE_STATUSES = {"pending_context", "pending_confirmation"}
INTERACTIVE_CONTEXT_FIELDS = {"semester_start_date", "period_time_mapping"}
DEFAULT_RUN_LEASE_SECONDS = 10 * 60


class UnfillableScheduleContextError(ValueError):
    def __init__(self, missing: set[str]):
        self.missing = frozenset(missing)
        super().__init__("schedule contains context that cannot be completed in V1")


class CourseCorrectionAmbiguityError(ValueError):
    """A participant-owned draft selector did not resolve to exactly one course."""

    def __init__(self, candidates: list[dict[str, Any]]):
        self.candidates = tuple(dict(value) for value in candidates[:10])
        super().__init__("correction must identify exactly one course")


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
        unsupported = missing - INTERACTIVE_CONTEXT_FIELDS
        if unsupported:
            raise UnfillableScheduleContextError(unsupported)
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
                if sources[index] == "image":
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
                preserve_image_time = bool(
                    sources[index] == "image"
                    and course.get("start_time")
                    and course.get("end_time")
                )
                if not preserve_image_time:
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
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            self._expire_if_needed(row, session)
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
        with self.database.session() as session:
            row = session.get(
                CourseScheduleImport, uuid.UUID(str(import_id)), with_for_update=True
            )
            self._require_owner(row, participant_id)
            expires = _aware(row.expires_at)
            if expires <= datetime.now(timezone.utc) and row.status in EXPIRABLE_STATUSES:
                row.status = "expired"
                self._clear_run_lease(row)
            if row.status == "expired":
                session.flush()
                return self._view(session, row)
            if row.status == "running":
                raise ValueError("running draft cannot be cancelled")
            if row.status not in {"succeeded", "expired"}:
                row.status = "cancelled"
                row.completed_at = datetime.now(timezone.utc)
                self._clear_run_lease(row)
            session.flush()
            return self._view(session, row)

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
            "recurrence_strategy": row.recurrence_strategy,
            "recurrence_confirmed_at": (
                _aware(row.recurrence_confirmed_at).isoformat()
                if row.recurrence_confirmed_at else None
            ),
            "created_at": _aware(row.created_at).isoformat(),
            "expires_at": _aware(row.expires_at).isoformat(),
            "confirmed_at": _aware(row.confirmed_at).isoformat() if row.confirmed_at else None,
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
    return structured
