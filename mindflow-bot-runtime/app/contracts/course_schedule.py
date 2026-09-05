"""Strict backend contracts for course-schedule vision results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class ScheduleVisionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WeekRule:
    start_week: int | None
    end_week: int | None
    odd_even: str = "all"
    explicit_weeks: tuple[int, ...] | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "WeekRule":
        if not isinstance(value, dict):
            raise ScheduleVisionValidationError("week_rule must be an object")
        allowed = {"start_week", "end_week", "odd_even", "explicit_weeks"}
        if set(value) - allowed:
            raise ScheduleVisionValidationError("week_rule contains unknown fields")
        odd_even = str(value.get("odd_even") or "all").lower()
        if odd_even not in {"all", "odd", "even"}:
            raise ScheduleVisionValidationError("odd_even is invalid")
        explicit = value.get("explicit_weeks")
        explicit_weeks = None
        if explicit is not None:
            if not isinstance(explicit, list) or not explicit:
                raise ScheduleVisionValidationError("explicit_weeks must be a non-empty list")
            try:
                explicit_weeks = tuple(int(item) for item in explicit)
            except (TypeError, ValueError) as exc:
                raise ScheduleVisionValidationError("explicit_weeks must contain integers") from exc
            if any(week < 1 or week > 60 for week in explicit_weeks):
                raise ScheduleVisionValidationError("explicit week is out of range")
            if len(set(explicit_weeks)) != len(explicit_weeks):
                raise ScheduleVisionValidationError("explicit_weeks contains duplicates")
            explicit_weeks = tuple(sorted(explicit_weeks))
        start = _optional_int(value.get("start_week"), "start_week", 1, 60)
        end = _optional_int(value.get("end_week"), "end_week", 1, 60)
        if explicit_weeks is None:
            if start is None or end is None:
                raise ScheduleVisionValidationError("week range is missing")
            if end < start:
                raise ScheduleVisionValidationError("week range is reversed")
        return cls(start, end, odd_even, explicit_weeks)


@dataclass(frozen=True)
class CourseScheduleItem:
    course_name: str
    weekday: int | None
    period_start: int | None
    period_end: int | None
    start_time: str | None
    end_time: str | None
    location: str | None
    teacher: str | None
    week_rule: WeekRule
    uncertain_fields: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, value: Any) -> "CourseScheduleItem":
        if not isinstance(value, dict):
            raise ScheduleVisionValidationError("course must be an object")
        allowed = {
            "course_name", "weekday", "period_start", "period_end", "start_time",
            "end_time", "location", "teacher", "week_rule", "uncertain_fields",
        }
        if set(value) - allowed:
            raise ScheduleVisionValidationError("course contains unknown fields")
        name = str(value.get("course_name") or "").strip()
        if not name or len(name) > 200:
            raise ScheduleVisionValidationError("course_name is invalid")
        weekday = _optional_int(value.get("weekday"), "weekday", 1, 7)
        period_start = _optional_int(value.get("period_start"), "period_start", 1, 30)
        period_end = _optional_int(value.get("period_end"), "period_end", 1, 30)
        if (period_start is None) != (period_end is None):
            raise ScheduleVisionValidationError("period range must be complete")
        if period_start is not None and period_end < period_start:
            raise ScheduleVisionValidationError("period range is reversed")
        start_time = _optional_time(value.get("start_time"), "start_time")
        end_time = _optional_time(value.get("end_time"), "end_time")
        if (start_time is None) != (end_time is None):
            raise ScheduleVisionValidationError("actual time range must be complete")
        if start_time is not None and end_time <= start_time:
            raise ScheduleVisionValidationError("actual time range is invalid")
        uncertain = value.get("uncertain_fields") or []
        if not isinstance(uncertain, list) or any(not isinstance(item, str) for item in uncertain):
            raise ScheduleVisionValidationError("uncertain_fields must be a string list")
        return cls(
            course_name=name,
            weekday=weekday,
            period_start=period_start,
            period_end=period_end,
            start_time=start_time,
            end_time=end_time,
            location=_optional_text(value.get("location"), 300),
            teacher=_optional_text(value.get("teacher"), 200),
            week_rule=WeekRule.from_dict(value.get("week_rule")),
            uncertain_fields=tuple(str(item)[:64] for item in uncertain),
        )


@dataclass(frozen=True)
class ScheduleVisionResult:
    document_type: str
    semester_label: str | None
    institution: str | None
    courses: tuple[CourseScheduleItem, ...]
    missing_context: tuple[str, ...]
    warnings: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, *, max_items: int = 80) -> "ScheduleVisionResult":
        if not isinstance(value, dict):
            raise ScheduleVisionValidationError("vision result must be an object")
        allowed = {
            "document_type", "semester_label", "institution", "courses",
            "missing_context", "warnings",
        }
        if set(value) != allowed:
            raise ScheduleVisionValidationError("vision result fields do not match schema")
        document_type = str(value.get("document_type") or "").strip()
        if document_type not in {"course_schedule", "not_course_schedule"}:
            raise ScheduleVisionValidationError("document_type is invalid")
        courses_raw = value.get("courses")
        if not isinstance(courses_raw, list):
            raise ScheduleVisionValidationError("courses must be a list")
        if len(courses_raw) > max_items:
            raise ScheduleVisionValidationError("course item limit exceeded")
        courses = tuple(CourseScheduleItem.from_dict(item) for item in courses_raw)
        if document_type == "course_schedule" and not courses:
            raise ScheduleVisionValidationError("course schedule contains no courses")
        if document_type == "not_course_schedule" and courses:
            raise ScheduleVisionValidationError("non-schedule image cannot contain courses")
        missing = _string_list(value.get("missing_context"), "missing_context", 64)
        known_missing = {
            "semester_start_date", "period_time_mapping", "weekday", "week_rule",
            "actual_time",
        }
        if any(item not in known_missing for item in missing):
            raise ScheduleVisionValidationError("missing_context contains unknown value")
        warnings = _string_list(value.get("warnings"), "warnings", 500)
        return cls(
            document_type=document_type,
            semester_label=_optional_text(value.get("semester_label"), 100),
            institution=_optional_text(value.get("institution"), 200),
            courses=courses,
            missing_context=tuple(missing),
            warnings=tuple(warnings),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["courses"] = [
            {
                **asdict(course),
                "week_rule": {
                    **asdict(course.week_rule),
                    "explicit_weeks": (
                        list(course.week_rule.explicit_weeks)
                        if course.week_rule.explicit_weeks is not None else None
                    ),
                },
                "uncertain_fields": list(course.uncertain_fields),
            }
            for course in self.courses
        ]
        result["missing_context"] = list(self.missing_context)
        result["warnings"] = list(self.warnings)
        return result


def _optional_int(value: Any, field: str, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ScheduleVisionValidationError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleVisionValidationError(f"{field} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ScheduleVisionValidationError(f"{field} is out of range")
    return result


def _optional_time(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleVisionValidationError(f"{field} must be HH:MM")
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError as exc:
        raise ScheduleVisionValidationError(f"{field} must be HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleVisionValidationError(f"{field} must be HH:MM")
    return f"{hour:02d}:{minute:02d}"


def _optional_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:maximum] or None


def _string_list(value: Any, field: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ScheduleVisionValidationError(f"{field} must be a string list")
    return [str(item).strip()[:maximum] for item in value if str(item).strip()]
