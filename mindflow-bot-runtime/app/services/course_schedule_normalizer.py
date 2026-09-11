"""Deterministic normalization for loose course-schedule vision JSON."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Mapping

from app.contracts.course_schedule import (
    CourseScheduleItem,
    ScheduleVisionResult,
    ScheduleVisionValidationError,
)


_TOP_LEVEL_FIELDS = frozenset(
    {
        "document_type",
        "semester_label",
        "institution",
        "courses",
        "missing_context",
        "warnings",
    }
)
_COURSE_FIELDS = frozenset(
    {
        "course_name",
        "weekday",
        "period_start",
        "period_end",
        "start_time",
        "end_time",
        "location",
        "teacher",
        "week_rule",
        "period_inference_source",
        "period_confidence",
        "uncertain_fields",
    }
)
_KNOWN_MISSING = frozenset(
    {
        "semester_start_date",
        "period_time_mapping",
        "weekday",
        "week_rule",
        "actual_time",
    }
)
_WEEKDAYS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
}
_WEEK_LOCATION = re.compile(
    r"^\s*(\d{1,2})\s*[-–—~至]\s*(\d{1,2})\s*周?"
    r"\s*(单周|双周|单双周)?\s*[,，;；]\s*(.+?)\s*$"
)
_WEEK_RANGE = re.compile(
    r"^\s*(\d{1,2})\s*[-–—~至]\s*(\d{1,2})\s*周?"
    r"\s*(单周|双周|单双周)?\s*$"
)


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value.strip()[:300]
    if isinstance(value, Mapping):
        return {
            str(key)[:64]: _safe_value(child)
            for key, child in list(value.items())[:12]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(child) for child in list(value)[:12]]
    return str(value)[:300]


def _issue(path: str, reason: str, **values: Any) -> dict[str, Any]:
    return {
        "path": path,
        "reason": reason,
        **{key: _safe_value(value) for key, value in values.items()},
    }


@dataclass(frozen=True)
class ParseReport:
    fixed: tuple[dict[str, Any], ...] = ()
    dropped: tuple[dict[str, Any], ...] = ()
    quarantined: tuple[dict[str, Any], ...] = ()
    missing: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixed": [dict(item) for item in self.fixed],
            "dropped": [dict(item) for item in self.dropped],
            "quarantined": [dict(item) for item in self.quarantined],
            "missing": list(self.missing),
        }


class CourseScheduleNormalizationError(ScheduleVisionValidationError):
    def __init__(self, message: str, report: ParseReport):
        self.report = report
        super().__init__(message)


def _coerce_int(value: Any, minimum: int, maximum: int) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        number = int(text)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def _coerce_weekday(value: Any) -> int | None:
    number = _coerce_int(value, 1, 7)
    if number is not None:
        return number
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:周|星期|礼拜)([一二三四五六日天])", text)
    return _WEEKDAYS.get(match.group(1)) if match else None


def _coerce_time(value: Any) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", str(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _odd_even(value: Any) -> str:
    text = str(value or "all").strip().lower()
    return {
        "单": "odd",
        "单周": "odd",
        "odd": "odd",
        "双": "even",
        "双周": "even",
        "even": "even",
        "单双周": "all",
        "all": "all",
    }.get(text, "all")


def _week_rule_from_text(value: str) -> tuple[dict[str, Any] | None, str | None]:
    match = _WEEK_LOCATION.fullmatch(value)
    location = match.group(4).strip() if match else None
    if match is None:
        match = _WEEK_RANGE.fullmatch(value)
    if match is None:
        return None, None
    start, end = int(match.group(1)), int(match.group(2))
    if not (1 <= start <= end <= 60):
        return None, location
    return {
        "start_week": start,
        "end_week": end,
        "odd_even": _odd_even(match.group(3)),
        "explicit_weeks": None,
    }, location


def _coerce_week_rule(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(value, str):
        return _week_rule_from_text(value)
    if not isinstance(value, Mapping):
        return None, None
    explicit_raw = value.get("explicit_weeks")
    explicit: list[int] | None = None
    if isinstance(explicit_raw, (list, tuple)):
        parsed = [_coerce_int(item, 1, 60) for item in explicit_raw]
        if parsed and all(item is not None for item in parsed):
            explicit = sorted(set(int(item) for item in parsed if item is not None))
    start = _coerce_int(value.get("start_week"), 1, 60)
    end = _coerce_int(value.get("end_week"), 1, 60)
    if explicit:
        return {
            "start_week": start,
            "end_week": end,
            "odd_even": _odd_even(value.get("odd_even")),
            "explicit_weeks": explicit,
        }, None
    if start is None or end is None or end < start:
        return None, None
    return {
        "start_week": start,
        "end_week": end,
        "odd_even": _odd_even(value.get("odd_even")),
        "explicit_weeks": None,
    }, None


def _normalize_course(
    raw: Any,
    index: int,
    fixed: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
) -> dict[str, Any]:
    path = f"courses[{index}]"
    if not isinstance(raw, Mapping):
        raise ScheduleVisionValidationError("course must be an object")
    name = str(raw.get("course_name") or "").strip()
    if not name:
        raise ScheduleVisionValidationError("course_name is missing")

    unknown = sorted(set(raw) - _COURSE_FIELDS)
    if unknown:
        dropped.append(_issue(path, "unknown_course_fields", fields=unknown))

    weekday = _coerce_weekday(raw.get("weekday"))
    if raw.get("weekday") is not None and weekday != raw.get("weekday"):
        fixed.append(
            _issue(
                f"{path}.weekday",
                "coerced_weekday",
                original=raw.get("weekday"),
                normalized=weekday,
            )
        )

    period_start = _coerce_int(raw.get("period_start"), 1, 30)
    period_end = _coerce_int(raw.get("period_end"), 1, 30)
    if period_start is None or period_end is None or period_end < period_start:
        if raw.get("period_start") is not None or raw.get("period_end") is not None:
            fixed.append(_issue(f"{path}.period", "incomplete_or_invalid_period_cleared"))
        period_start = period_end = None
    elif period_start != raw.get("period_start") or period_end != raw.get("period_end"):
        fixed.append(_issue(f"{path}.period", "coerced_period_numbers"))

    start_time = _coerce_time(raw.get("start_time"))
    end_time = _coerce_time(raw.get("end_time"))
    if (
        start_time is None
        or end_time is None
        or end_time <= start_time
    ):
        if raw.get("start_time") is not None or raw.get("end_time") is not None:
            fixed.append(_issue(f"{path}.time", "incomplete_or_invalid_time_cleared"))
        start_time = end_time = None
    elif start_time != raw.get("start_time") or end_time != raw.get("end_time"):
        fixed.append(_issue(f"{path}.time", "normalized_time_format"))

    location = str(raw.get("location") or "").strip()[:300] or None
    week_rule, embedded_location = _coerce_week_rule(raw.get("week_rule"))
    if week_rule is None and location:
        location_rule, location_remainder = _week_rule_from_text(location)
        if location_rule is not None:
            week_rule = location_rule
            location = location_remainder
            fixed.append(_issue(f"{path}.location", "split_week_rule_and_location"))
    elif embedded_location:
        if not location:
            location = embedded_location[:300]
        fixed.append(_issue(f"{path}.week_rule", "split_week_rule_and_location"))
    if raw.get("week_rule") is not None and week_rule is None:
        fixed.append(_issue(f"{path}.week_rule", "invalid_or_partial_week_rule_cleared"))

    uncertain_raw = raw.get("uncertain_fields")
    uncertain = (
        [str(item).strip()[:64] for item in uncertain_raw if str(item).strip()]
        if isinstance(uncertain_raw, list)
        else []
    )
    for field_name, missing in (
        ("weekday", weekday is None),
        ("week_rule", week_rule is None),
        ("actual_time", start_time is None and period_start is None),
    ):
        if missing and field_name not in uncertain:
            uncertain.append(field_name)

    inference_source = str(raw.get("period_inference_source") or "unknown").strip().lower()
    if inference_source not in {"explicit_label", "cell_text", "grid_position", "unknown"}:
        fixed.append(_issue(f"{path}.period_inference_source", "invalid_inference_source"))
        inference_source = "unknown"
    confidence = raw.get("period_confidence")
    try:
        confidence = (
            None
            if confidence is None or isinstance(confidence, bool)
            else float(confidence)
        )
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None and not 0 <= confidence <= 1:
        confidence = None
    if raw.get("period_confidence") is not None and confidence is None:
        fixed.append(_issue(f"{path}.period_confidence", "invalid_confidence_cleared"))

    return {
        "course_name": name[:200],
        "weekday": weekday,
        "period_start": period_start,
        "period_end": period_end,
        "start_time": start_time,
        "end_time": end_time,
        "location": location,
        "teacher": str(raw.get("teacher") or "").strip()[:200] or None,
        "week_rule": week_rule,
        "period_inference_source": inference_source,
        "period_confidence": confidence,
        "uncertain_fields": uncertain,
    }


def normalize_course_schedule(
    value: Any, *, max_items: int = 20
) -> ScheduleVisionResult:
    """Coerce safe formatting differences and isolate invalid course rows."""

    fixed: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    missing: set[str] = set()
    if not isinstance(value, Mapping):
        report = ParseReport()
        raise CourseScheduleNormalizationError(
            "vision result must be an object", report
        )

    unknown = sorted(set(value) - _TOP_LEVEL_FIELDS)
    if unknown:
        dropped.append(_issue("$", "unknown_top_level_fields", fields=unknown))
    document_type = str(value.get("document_type") or "course_schedule").strip().lower()
    if document_type not in {"course_schedule", "not_course_schedule"}:
        document_type = "course_schedule"
        fixed.append(_issue("document_type", "invalid_document_type_defaulted"))

    raw_missing = value.get("missing_context")
    if isinstance(raw_missing, list):
        for item in raw_missing:
            text = str(item).strip()
            if text in _KNOWN_MISSING:
                missing.add(text)
            elif text:
                dropped.append(_issue("missing_context", "unknown_missing_context", value=text))
    elif raw_missing is not None:
        dropped.append(_issue("missing_context", "invalid_missing_context_dropped"))

    warnings_raw = value.get("warnings")
    warnings = (
        [str(item).strip()[:500] for item in warnings_raw if str(item).strip()]
        if isinstance(warnings_raw, list)
        else []
    )
    if warnings_raw is not None and not isinstance(warnings_raw, list):
        dropped.append(_issue("warnings", "invalid_warnings_dropped"))

    raw_courses = value.get("courses")
    if not isinstance(raw_courses, list):
        raw_courses = []
        dropped.append(_issue("courses", "invalid_courses_replaced"))
    normalized_courses: list[dict[str, Any]] = []
    effective_limit = min(20, max(1, int(max_items)))
    for index, raw_course in enumerate(raw_courses):
        if len(normalized_courses) >= effective_limit:
            quarantined.append(
                _issue(
                    f"courses[{index}]",
                    "course_item_limit_exceeded",
                    course_name=(
                        raw_course.get("course_name")
                        if isinstance(raw_course, Mapping)
                        else None
                    ),
                )
            )
            continue
        try:
            normalized = _normalize_course(raw_course, index, fixed, dropped)
            CourseScheduleItem.from_dict(normalized)
        except ScheduleVisionValidationError as exc:
            quarantined.append(
                _issue(
                    f"courses[{index}]",
                    "course_validation_failed",
                    course_name=(
                        raw_course.get("course_name")
                        if isinstance(raw_course, Mapping)
                        else None
                    ),
                    detail=str(exc)[:200],
                )
            )
            continue
        normalized_courses.append(normalized)

    if any(course["weekday"] is None for course in normalized_courses):
        missing.add("weekday")
    if any(course["week_rule"] is None for course in normalized_courses):
        missing.add("week_rule")
    if any(
        course["start_time"] is None and course["period_start"] is None
        for course in normalized_courses
    ):
        missing.add("actual_time")

    report = ParseReport(
        fixed=tuple(fixed),
        dropped=tuple(dropped),
        quarantined=tuple(quarantined),
        missing=tuple(sorted(missing)),
    )
    strict_payload = {
        "document_type": document_type,
        "semester_label": value.get("semester_label"),
        "institution": value.get("institution"),
        "courses": normalized_courses,
        "missing_context": sorted(missing),
        "warnings": warnings,
    }
    try:
        result = ScheduleVisionResult.from_dict(
            strict_payload, max_items=effective_limit
        )
    except ScheduleVisionValidationError as exc:
        raise CourseScheduleNormalizationError(str(exc), report) from exc
    return replace(result, parse_report=report.to_dict())
