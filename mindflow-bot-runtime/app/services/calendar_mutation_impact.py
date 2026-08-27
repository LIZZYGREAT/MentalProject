"""Resolve persisted Forecast dates affected by one Calendar mutation."""

from __future__ import annotations

import calendar as month_calendar
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


_WEEKDAYS = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}
_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
_ALLOWED_RECURRENCE_KEYS = {"FREQ", "INTERVAL", "COUNT", "BYDAY", "UNTIL"}
_MAX_REVIEWED_EVENT_DATE_SPAN = 31


class UnsupportedCalendarRecurrence(ValueError):
    """The provider rule is outside MindFlow's reviewed RFC5545 subset."""


class CalendarMutationImpactResolver:
    """Match only the reviewed RFC5545 subset emitted by Calendar tools."""

    def __init__(self, timezone_name: str):
        self.timezone = ZoneInfo(timezone_name)

    def affected_dates(
        self,
        *,
        previous: dict[str, Any] | None,
        updated: dict[str, Any] | None,
        persisted_dates: Iterable[date],
        updated_recurrence: str | None = None,
        clear_recurrence: bool = False,
    ) -> set[date]:
        candidates = set(persisted_dates)
        result = self.covered_dates(previous) | self.covered_dates(updated)
        result.update(
            target
            for target in candidates
            if self._event_affects_date(previous, target)
        )
        effective_updated = dict(updated or {})
        if clear_recurrence:
            effective_updated["recurrence"] = ""
        elif updated_recurrence is not None:
            effective_updated["recurrence"] = updated_recurrence
        result.update(
            target
            for target in candidates
            if self._event_affects_date(effective_updated, target)
        )
        return result

    def covered_dates(self, event: dict[str, Any] | None) -> set[date]:
        interval = self._interval(event)
        if interval is None:
            return set()
        start, end = interval
        last = (end - timedelta(microseconds=1)).date()
        if (last - start.date()).days > _MAX_REVIEWED_EVENT_DATE_SPAN:
            # External providers may return events beyond MindFlow's reviewed
            # duration. Those are matched only against persisted dates below.
            return set()
        return {
            start.date() + timedelta(days=offset)
            for offset in range((last - start.date()).days + 1)
        }

    def _event_affects_date(
        self, event: dict[str, Any] | None, target: date
    ) -> bool:
        interval = self._interval(event)
        if interval is None:
            return False
        start, end = interval
        local_date_span = (end.date() - start.date()).days
        try:
            rule = self._parse_rule((event or {}).get("recurrence"))
        except UnsupportedCalendarRecurrence:
            # Existing calendars may contain selectors that MindFlow never
            # emits. Never reinterpret those as a different recurrence. The
            # caller only supplies dates with persisted valid Forecasts, so
            # conservatively matching every date from DTSTART remains bounded.
            return target >= start.date()
        if rule is None:
            target_start = datetime.combine(target, time.min, self.timezone)
            target_end = (
                None
                if target == date.max
                else target_start + timedelta(days=1)
            )
            return (target_end is None or start < target_end) and end > target_start
        if local_date_span > _MAX_REVIEWED_EVENT_DATE_SPAN:
            return target >= start.date()

        duration = end - start
        target_start = datetime.combine(target, time.min, self.timezone)
        target_end = (
            None if target == date.max else target_start + timedelta(days=1)
        )
        # Use the actual local-date span. This keeps overlap correct at the
        # exact 31-day boundary without relying on CalendarService validation.
        lookback_days = max(0, local_date_span)
        for offset in range(lookback_days + 1):
            if offset > (target - date.min).days:
                break
            occurrence_date = target - timedelta(days=offset)
            if not self._is_occurrence_date(start, occurrence_date, rule):
                continue
            occurrence_start = datetime.combine(
                occurrence_date, start.timetz(), self.timezone
            )
            try:
                occurrence_end = occurrence_start + duration
            except OverflowError:
                occurrence_end = datetime.combine(
                    date.max, time.max, self.timezone
                )
            if (
                (target_end is None or occurrence_start < target_end)
                and occurrence_end > target_start
            ):
                return True
        return False

    def _interval(
        self, event: dict[str, Any] | None
    ) -> tuple[datetime, datetime] | None:
        if not event:
            return None
        try:
            start = self._datetime(event.get("start_time"))
            end = self._datetime(event.get("end_time"))
        except (TypeError, ValueError):
            return None
        if end <= start:
            return None
        return start, end

    def _datetime(self, value: Any) -> datetime:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return parsed.astimezone(self.timezone)

    @staticmethod
    def _parse_rule(value: Any) -> dict[str, Any] | None:
        text = str(value or "").strip().upper()
        if not text:
            return None
        parts: dict[str, str] = {}
        for item in text.split(";"):
            key, separator, raw = item.partition("=")
            if (
                not separator
                or not key
                or not raw
                or key not in _ALLOWED_RECURRENCE_KEYS
                or key in parts
            ):
                raise UnsupportedCalendarRecurrence(text)
            parts[key] = raw
        frequency = parts.get("FREQ")
        if frequency not in _FREQUENCIES:
            raise UnsupportedCalendarRecurrence(text)
        try:
            interval = int(parts.get("INTERVAL", "1"))
            count = int(parts["COUNT"]) if "COUNT" in parts else None
        except ValueError:
            raise UnsupportedCalendarRecurrence(text)
        if not 1 <= interval <= 99 or (count is not None and not 1 <= count <= 999):
            raise UnsupportedCalendarRecurrence(text)
        if count is not None and "UNTIL" in parts:
            raise UnsupportedCalendarRecurrence(text)
        raw_weekdays = parts.get("BYDAY", "").split(",")
        if any(item not in _WEEKDAYS for item in raw_weekdays if item):
            raise UnsupportedCalendarRecurrence(text)
        weekdays = tuple(_WEEKDAYS[item] for item in raw_weekdays if item)
        if weekdays and frequency != "WEEKLY":
            raise UnsupportedCalendarRecurrence(text)
        until = None
        if "UNTIL" in parts:
            try:
                until = datetime.strptime(parts["UNTIL"], "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                raise UnsupportedCalendarRecurrence(text)
        return {
            "frequency": frequency,
            "interval": interval,
            "count": count,
            "weekdays": weekdays,
            "until": until,
        }

    def _is_occurrence_date(
        self,
        start: datetime,
        candidate: date,
        rule: dict[str, Any],
    ) -> bool:
        if candidate < start.date():
            return False
        candidate_start = datetime.combine(candidate, start.timetz(), self.timezone)
        until = rule["until"]
        if until is not None and candidate_start.astimezone(timezone.utc) > until:
            return False
        occurrence_index = self._occurrence_index(start, candidate, rule)
        if occurrence_index is None:
            return False
        count = rule["count"]
        return count is None or occurrence_index < int(count)

    @staticmethod
    def _occurrence_index(
        start: datetime,
        candidate: date,
        rule: dict[str, Any],
    ) -> int | None:
        """Return the zero-based ordinal without expanding dates after target."""

        frequency = rule["frequency"]
        interval = int(rule["interval"])
        if frequency == "DAILY":
            delta_days = (candidate - start.date()).days
            return delta_days // interval if delta_days % interval == 0 else None
        if frequency == "WEEKLY":
            weekdays = sorted(set(rule["weekdays"] or (start.weekday(),)))
            base_week = start.date() - timedelta(days=start.weekday())
            week_delta = (candidate - base_week).days // 7
            if week_delta % interval != 0 or candidate.weekday() not in weekdays:
                return None
            active_week = week_delta // interval
            if active_week == 0:
                return len(
                    [
                        weekday
                        for weekday in weekdays
                        if start.weekday() <= weekday < candidate.weekday()
                    ]
                )
            first_week_count = len(
                [weekday for weekday in weekdays if weekday >= start.weekday()]
            )
            return (
                first_week_count
                + (active_week - 1) * len(weekdays)
                + len([weekday for weekday in weekdays if weekday < candidate.weekday()])
            )
        if frequency == "MONTHLY":
            month_delta = (
                (candidate.year - start.year) * 12
                + candidate.month
                - start.month
            )
            if month_delta % interval != 0 or candidate.day != start.day:
                return None
            ordinal = 0
            for scheduled_delta in range(0, month_delta, interval):
                total_month = (
                    start.year * 12 + start.month - 1 + scheduled_delta
                )
                year, month_zero = divmod(total_month, 12)
                if start.day <= month_calendar.monthrange(year, month_zero + 1)[1]:
                    ordinal += 1
            return ordinal

        year_delta = candidate.year - start.year
        if (
            year_delta % interval != 0
            or candidate.month != start.month
            or candidate.day != start.day
        ):
            return None
        if (start.month, start.day) != (2, 29):
            return year_delta // interval
        return sum(
            1
            for scheduled_delta in range(0, year_delta, interval)
            if month_calendar.isleap(start.year + scheduled_delta)
        )
