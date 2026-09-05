"""Pure deterministic Calendar-write planning for course schedule imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.integrations.feishu.calendar import build_recurrence_rule


COURSE_IMPORT_PLANNER_VERSION = "v2"
PRESERVE_SCHEDULE_PATTERN = "preserve_schedule_pattern"
EXPAND_ALL_OCCURRENCES = "expand_all_occurrences"
RECURRENCE_STRATEGIES = {
    PRESERVE_SCHEDULE_PATTERN,
    EXPAND_ALL_OCCURRENCES,
}
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class CalendarWriteKind:
    SINGLE = "single"
    RECURRING = "recurring"


@dataclass(frozen=True)
class CalendarWrite:
    summary: str
    start_time: datetime
    end_time: datetime
    description: str
    write_kind: str
    recurrence: str | None
    occurrence_identity: str
    affected_dates: tuple[date, ...]


def plan_course_writes(
    draft: dict[str, Any],
    item: dict[str, Any],
    *,
    strategy: str,
    timezone: ZoneInfo,
) -> list[CalendarWrite]:
    if strategy not in RECURRENCE_STRATEGIES:
        raise ValueError("unsupported course recurrence strategy")
    if not draft.get("semester_start_date"):
        raise ValueError("semester_start_date is required")
    if item.get("weekday") is None or not item.get("start_time") or not item.get("end_time"):
        raise ValueError("course item is missing actual date or time context")
    semester_monday = date.fromisoformat(draft["semester_start_date"])
    if semester_monday.weekday() != 0:
        raise ValueError("semester_start_date must be a Monday")
    weekday = int(item["weekday"])
    rule = dict(item.get("week_rule") or {})
    weeks = course_weeks(rule)
    start_clock = time.fromisoformat(item["start_time"])
    end_clock = time.fromisoformat(item["end_time"])
    description = "\n".join(
        value
        for value in (
            f"地点：{item['location']}" if item.get("location") else "",
            "由 MindFlow 课程表导入",
        )
        if value
    )
    dates = tuple(
        semester_monday + timedelta(weeks=week - 1, days=weekday - 1)
        for week in weeks
    )

    if strategy == EXPAND_ALL_OCCURRENCES or len(weeks) == 1:
        return _single_writes(item, weeks, dates, start_clock, end_clock, timezone, description)

    interval = _stable_interval(weeks)
    if rule.get("explicit_weeks") is not None and interval is None:
        return _single_writes(item, weeks, dates, start_clock, end_clock, timezone, description)
    if interval is None:
        raise ValueError("course week rule is not a stable recurrence")
    recurrence = build_recurrence_rule(
        "WEEKLY",
        interval=interval,
        weekdays=[_WEEKDAYS[weekday - 1]],
        count=len(weeks),
    )
    return [CalendarWrite(
        summary=item["course_name"],
        start_time=datetime.combine(dates[0], start_clock, timezone),
        end_time=datetime.combine(dates[0], end_clock, timezone),
        description=description,
        write_kind=CalendarWriteKind.RECURRING,
        recurrence=recurrence,
        occurrence_identity=f"series-{weeks[0]}-{weeks[-1]}-interval-{interval}",
        affected_dates=dates,
    )]


def describe_course_write_plan(writes: list[CalendarWrite]) -> str:
    if len(writes) == 1 and writes[0].write_kind == CalendarWriteKind.RECURRING:
        recurrence = str(writes[0].recurrence or "")
        return "每两周重复" if "INTERVAL=2" in recurrence else "每周重复"
    if len(writes) == 1:
        return "仅一次"
    return "指定周次，将按单次日程添加"


def course_weeks(rule: dict[str, Any]) -> list[int]:
    explicit = rule.get("explicit_weeks")
    if explicit is not None:
        result = sorted({int(value) for value in explicit})
    else:
        start = int(rule["start_week"])
        end = int(rule["end_week"])
        odd_even = str(rule.get("odd_even") or "all")
        result = [
            week
            for week in range(start, end + 1)
            if odd_even == "all"
            or (odd_even == "odd" and week % 2 == 1)
            or (odd_even == "even" and week % 2 == 0)
        ]
    if not result:
        raise ValueError("week rule has no occurrences")
    return result


def _stable_interval(weeks: list[int]) -> int | None:
    if len(weeks) < 2:
        return None
    intervals = {later - earlier for earlier, later in zip(weeks, weeks[1:])}
    if len(intervals) != 1:
        return None
    interval = intervals.pop()
    return interval if 1 <= interval <= 99 else None


def _single_writes(
    item: dict[str, Any],
    weeks: list[int],
    dates: tuple[date, ...],
    start_clock: time,
    end_clock: time,
    timezone: ZoneInfo,
    description: str,
) -> list[CalendarWrite]:
    return [
        CalendarWrite(
            summary=item["course_name"],
            start_time=datetime.combine(target, start_clock, timezone),
            end_time=datetime.combine(target, end_clock, timezone),
            description=description,
            write_kind=CalendarWriteKind.SINGLE,
            recurrence=None,
            occurrence_identity=f"week-{week}",
            affected_dates=(target,),
        )
        for week, target in zip(weeks, dates)
    ]
