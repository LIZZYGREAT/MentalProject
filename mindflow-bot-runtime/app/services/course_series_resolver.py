"""Backend-owned course-series identity and semester-scope resolution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import re
from typing import Any, Literal
import uuid
from zoneinfo import ZoneInfo

from app.domain.course_schedule_recurrence import course_weeks


CourseSeriesScope = Literal[
    "single_occurrence",
    "current_semester_remainder",
    "entire_series",
]
CourseSeriesResolutionSource = Literal[
    "course_import",
    "provider_series",
    "deterministic_signature",
]

_WINDOW_DAYS = 31
COURSE_SERIES_MAX_OCCURRENCES = 64
_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class CourseSeriesResolutionError(ValueError):
    """Stable clarification boundary for a course-series request."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class ResolvedCourseSeries:
    anchor_event_id: str
    course_identity: str
    display_name: str
    scope_start: datetime
    scope_end: datetime
    occurrence_events: tuple[dict[str, Any], ...]
    resolution_source: CourseSeriesResolutionSource

    @property
    def occurrence_event_ids(self) -> tuple[str, ...]:
        return tuple(str(item["id"]) for item in self.occurrence_events)


def _event_datetime(value: Any, timezone_value: ZoneInfo) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CourseSeriesResolutionError("course_series_time_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_value)
    return parsed.astimezone(timezone_value)


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _recurrence_parts(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in str(value or "").strip().split(";"):
        name, separator, raw = part.partition("=")
        if separator:
            result[name.strip().upper()] = raw.strip().upper()
    return result


def _recurrence_end(recurrence: Any, *, series_start: datetime) -> datetime | None:
    parts = _recurrence_parts(recurrence)
    until = parts.get("UNTIL")
    if until:
        try:
            if re.fullmatch(r"\d{8}T\d{6}Z", until):
                parsed = datetime.strptime(until, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            elif re.fullmatch(r"\d{8}", until):
                parsed = datetime.combine(
                    datetime.strptime(until, "%Y%m%d").date(),
                    time(23, 59, 59),
                    series_start.tzinfo,
                )
            else:
                text = until[:-1] + "+00:00" if until.endswith("Z") else until
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=series_start.tzinfo)
            return parsed.astimezone(series_start.tzinfo)
        except ValueError:
            return None
    try:
        count = int(parts.get("COUNT") or 0)
        interval = int(parts.get("INTERVAL") or 1)
    except ValueError:
        return None
    if count < 1 or interval < 1:
        return None
    unit = {
        "DAILY": timedelta(days=interval),
        "WEEKLY": timedelta(weeks=interval),
    }.get(parts.get("FREQ"))
    return series_start + unit * (count - 1) if unit is not None else None


def _signature(event: dict[str, Any], timezone_value: ZoneInfo) -> str:
    start = _event_datetime(event.get("start_time"), timezone_value)
    material = "\0".join(
        (
            _normalized_text(event.get("summary")),
            _WEEKDAY_CODES[start.weekday()],
            start.strftime("%H:%M"),
            _normalized_text(event.get("location")),
            str(event.get("recurrence") or "").strip().upper(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CourseSeriesResolver:
    def __init__(
        self,
        calendar: Any,
        *,
        timezone_name: str,
        course_imports: Any = None,
    ):
        self.calendar = calendar
        self.timezone = ZoneInfo(timezone_name)
        self.course_imports = course_imports

    async def resolve(
        self,
        participant_id: uuid.UUID,
        *,
        anchor_event: dict[str, Any],
        scope: CourseSeriesScope,
        reference_local_date: date,
    ) -> ResolvedCourseSeries:
        if scope not in {
            "single_occurrence",
            "current_semester_remainder",
            "entire_series",
        }:
            raise CourseSeriesResolutionError("course_series_scope_invalid")
        anchor = dict(anchor_event or {})
        anchor_id = str(anchor.get("id") or "").strip()
        if not anchor_id:
            raise CourseSeriesResolutionError("course_series_anchor_not_found")
        anchor_start = _event_datetime(anchor.get("start_time"), self.timezone)
        anchor_end = _event_datetime(anchor.get("end_time"), self.timezone)
        if scope == "single_occurrence":
            return ResolvedCourseSeries(
                anchor_event_id=anchor_id,
                course_identity=f"occurrence:{anchor_id}",
                display_name=str(anchor.get("summary") or "未命名课程")[:200],
                scope_start=anchor_start,
                scope_end=anchor_end,
                occurrence_events=(anchor,),
                resolution_source="deterministic_signature",
            )

        provenance = await self._course_import_provenance(participant_id, anchor)
        if provenance is not None:
            return await self._resolve_imported(
                participant_id,
                anchor=anchor,
                provenance=provenance,
                scope=scope,
                reference_local_date=reference_local_date,
            )
        return await self._resolve_provider_series(
            participant_id,
            anchor=anchor,
            scope=scope,
            reference_local_date=reference_local_date,
        )

    async def _course_import_provenance(
        self, participant_id: uuid.UUID, anchor: dict[str, Any]
    ) -> dict[str, Any] | None:
        resolver = getattr(
            self.course_imports, "resolve_calendar_event_provenance", None
        )
        if not callable(resolver):
            return None
        identities = (
            str(anchor.get("id") or "").strip(),
            str(anchor.get("recurring_event_id") or "").strip(),
        )
        for identity in identities:
            if not identity:
                continue
            value = await asyncio.to_thread(resolver, participant_id, identity)
            if value is not None:
                return dict(value)
        return None

    async def _resolve_imported(
        self,
        participant_id: uuid.UUID,
        *,
        anchor: dict[str, Any],
        provenance: dict[str, Any],
        scope: CourseSeriesScope,
        reference_local_date: date,
    ) -> ResolvedCourseSeries:
        try:
            semester_start = date.fromisoformat(
                str(provenance["semester_start_date"])
            )
            weeks = course_weeks(dict(provenance.get("week_rule") or {}))
            weekday = int(provenance["weekday"])
            end_clock = time.fromisoformat(str(provenance["end_time"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CourseSeriesResolutionError(
                "course_semester_boundary_required"
            ) from exc
        semester_end_date = semester_start + timedelta(
            weeks=max(weeks) - 1, days=weekday - 1
        )
        scope_end = datetime.combine(semester_end_date, end_clock, self.timezone)
        anchor_start = _event_datetime(anchor.get("start_time"), self.timezone)
        scope_start = (
            datetime.combine(semester_start, time.min, self.timezone)
            if scope == "entire_series"
            else max(
                anchor_start,
                datetime.combine(reference_local_date, time.min, self.timezone),
            )
        )
        events = await self._events_between(participant_id, scope_start, scope_end)
        provider_ids = {
            str(value) for value in provenance.get("provider_event_ids") or ()
        }
        matches = [
            event
            for event in events
            if str(event.get("id") or "") in provider_ids
            or str(event.get("recurring_event_id") or "") in provider_ids
        ]
        return self._resolved(
            anchor=anchor,
            events=matches,
            identity=(
                f"course-import:{provenance['import_id']}:"
                f"{provenance['course_item_id']}"
            ),
            display_name=str(provenance.get("display_name") or anchor.get("summary")),
            scope_start=scope_start,
            scope_end=scope_end,
            source="course_import",
        )

    async def _resolve_provider_series(
        self,
        participant_id: uuid.UUID,
        *,
        anchor: dict[str, Any],
        scope: CourseSeriesScope,
        reference_local_date: date,
    ) -> ResolvedCourseSeries:
        series_id = str(anchor.get("recurring_event_id") or "").strip()
        series = dict(anchor)
        if series_id:
            series = dict(await self.calendar.get_event(participant_id, series_id) or {})
        elif str(anchor.get("recurrence") or "").strip():
            series_id = str(anchor.get("id") or "").strip()
        else:
            return await self._resolve_deterministic(
                participant_id,
                anchor=anchor,
                scope=scope,
                reference_local_date=reference_local_date,
            )
        series_start = _event_datetime(series.get("start_time"), self.timezone)
        series_end = _recurrence_end(
            series.get("recurrence"), series_start=series_start
        )
        if series_end is None:
            raise CourseSeriesResolutionError("course_semester_boundary_required")
        anchor_start = _event_datetime(anchor.get("start_time"), self.timezone)
        scope_start = (
            series_start
            if scope == "entire_series"
            else max(
                anchor_start,
                datetime.combine(reference_local_date, time.min, self.timezone),
            )
        )
        events = await self._events_between(participant_id, scope_start, series_end)
        matches = [
            event
            for event in events
            if str(event.get("recurring_event_id") or "") == series_id
            or str(event.get("id") or "") == series_id
        ]
        return self._resolved(
            anchor=anchor,
            events=matches,
            identity=f"provider-series:{series_id}",
            display_name=str(anchor.get("summary") or series.get("summary")),
            scope_start=scope_start,
            scope_end=series_end,
            source="provider_series",
        )

    async def _resolve_deterministic(
        self,
        participant_id: uuid.UUID,
        *,
        anchor: dict[str, Any],
        scope: CourseSeriesScope,
        reference_local_date: date,
    ) -> ResolvedCourseSeries:
        raw_end = anchor.get("semester_end_date") or anchor.get("semester_end")
        raw_start = anchor.get("semester_start_date") or anchor.get("semester_start")
        if not raw_end:
            raise CourseSeriesResolutionError("course_series_ambiguous")
        try:
            semester_end_date = date.fromisoformat(str(raw_end))
            semester_start_date = (
                date.fromisoformat(str(raw_start)) if raw_start else None
            )
        except ValueError as exc:
            raise CourseSeriesResolutionError(
                "course_semester_boundary_required"
            ) from exc
        if scope == "entire_series" and semester_start_date is None:
            raise CourseSeriesResolutionError("course_semester_boundary_required")
        anchor_start = _event_datetime(anchor.get("start_time"), self.timezone)
        anchor_end = _event_datetime(anchor.get("end_time"), self.timezone)
        scope_start = (
            datetime.combine(semester_start_date, time.min, self.timezone)
            if scope == "entire_series" and semester_start_date is not None
            else max(
                anchor_start,
                datetime.combine(reference_local_date, time.min, self.timezone),
            )
        )
        scope_end = datetime.combine(
            semester_end_date, anchor_end.timetz().replace(tzinfo=None), self.timezone
        )
        expected_signature = _signature(anchor, self.timezone)
        events = await self._events_between(participant_id, scope_start, scope_end)
        matches = [
            event
            for event in events
            if _signature(event, self.timezone) == expected_signature
        ]
        dates = [
            _event_datetime(event.get("start_time"), self.timezone).date()
            for event in matches
        ]
        if len(dates) != len(set(dates)):
            raise CourseSeriesResolutionError("course_series_ambiguous")
        return self._resolved(
            anchor=anchor,
            events=matches,
            identity=f"deterministic-signature:{expected_signature}",
            display_name=str(anchor.get("summary") or "未命名课程"),
            scope_start=scope_start,
            scope_end=scope_end,
            source="deterministic_signature",
        )

    async def _events_between(
        self,
        participant_id: uuid.UUID,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = start
        inclusive_end = end + timedelta(seconds=1)
        while cursor < inclusive_end:
            window_end = min(cursor + timedelta(days=_WINDOW_DAYS), inclusive_end)
            events.extend(
                dict(item)
                for item in await self.calendar.get_events(
                    participant_id, cursor, window_end
                )
            )
            cursor = window_end
        by_id: dict[str, dict[str, Any]] = {}
        for event in events:
            event_id = str(event.get("id") or "").strip()
            if event_id:
                by_id[event_id] = event
        return sorted(
            by_id.values(), key=lambda value: str(value.get("start_time") or "")
        )

    @staticmethod
    def _resolved(
        *,
        anchor: dict[str, Any],
        events: list[dict[str, Any]],
        identity: str,
        display_name: str,
        scope_start: datetime,
        scope_end: datetime,
        source: CourseSeriesResolutionSource,
    ) -> ResolvedCourseSeries:
        if not events:
            raise CourseSeriesResolutionError("course_series_occurrences_not_found")
        if len(events) > COURSE_SERIES_MAX_OCCURRENCES:
            raise CourseSeriesResolutionError("course_series_occurrence_limit_exceeded")
        return ResolvedCourseSeries(
            anchor_event_id=str(anchor.get("id") or ""),
            course_identity=identity,
            display_name=str(display_name or "未命名课程")[:200],
            scope_start=scope_start,
            scope_end=scope_end,
            occurrence_events=tuple(events),
            resolution_source=source,
        )
