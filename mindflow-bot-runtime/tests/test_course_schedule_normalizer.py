import pytest

from app.services.course_schedule_normalizer import (
    CourseScheduleNormalizationError,
    normalize_course_schedule,
)


def _course(name="高等数学"):
    return {
        "course_name": name,
        "weekday": 1,
        "period_start": 1,
        "period_end": 2,
        "start_time": "08:00",
        "end_time": "09:35",
        "location": "A101",
        "teacher": None,
        "week_rule": {
            "start_week": 1,
            "end_week": 16,
            "odd_even": "all",
            "explicit_weeks": None,
        },
        "period_inference_source": "cell_text",
        "period_confidence": 0.9,
        "uncertain_fields": [],
    }


def _payload(courses):
    return {
        "document_type": "course_schedule",
        "semester_label": None,
        "institution": None,
        "courses": courses,
        "missing_context": [],
        "warnings": [],
    }


def test_normalizer_coerces_production_style_fields_and_ignores_extra_keys():
    course = _course()
    course.update(
        {
            "weekday": "周三",
            "period_start": "3",
            "period_end": "4",
            "start_time": "8:00",
            "end_time": "9:35",
            "week_rule": None,
            "location": "1-17,津南公教楼D区402",
            "model_note": "ignore me",
        }
    )
    payload = _payload([course])
    payload["missing_context"] = ["location", "teacher", "semester_start_date"]
    payload["extra"] = True

    result = normalize_course_schedule(payload)

    normalized = result.courses[0]
    assert normalized.weekday == 3
    assert (normalized.period_start, normalized.period_end) == (3, 4)
    assert (normalized.start_time, normalized.end_time) == ("08:00", "09:35")
    assert normalized.location == "津南公教楼D区402"
    assert (normalized.week_rule.start_week, normalized.week_rule.end_week) == (1, 17)
    assert result.missing_context == ("semester_start_date",)
    assert result.parse_report["fixed"]
    assert {item["reason"] for item in result.parse_report["dropped"]} == {
        "unknown_top_level_fields",
        "unknown_missing_context",
        "unknown_course_fields",
    }


def test_normalizer_clears_half_time_and_partial_week_rule_to_missing_context():
    course = _course()
    course.update(
        {
            "period_start": None,
            "period_end": None,
            "start_time": "08:00",
            "end_time": None,
            "week_rule": {"start_week": "1", "end_week": None},
        }
    )

    result = normalize_course_schedule(_payload([course]))

    normalized = result.courses[0]
    assert normalized.start_time is normalized.end_time is None
    assert normalized.week_rule is None
    assert set(result.missing_context) == {"actual_time", "week_rule"}
    assert set(normalized.uncertain_fields) >= {"actual_time", "week_rule"}


def test_one_invalid_course_is_quarantined_without_discarding_valid_courses():
    result = normalize_course_schedule(
        _payload([_course("线性代数"), {"weekday": "周五"}])
    )

    assert [course.course_name for course in result.courses] == ["线性代数"]
    assert result.parse_report["quarantined"][0]["path"] == "courses[1]"
    assert result.parse_report["quarantined"][0]["reason"] == (
        "course_validation_failed"
    )


def test_courses_over_limit_are_quarantined_instead_of_rejecting_whole_image():
    result = normalize_course_schedule(
        _payload([_course(f"课程{index}") for index in range(21)])
    )

    assert len(result.courses) == 20
    assert result.parse_report["quarantined"][0]["reason"] == (
        "course_item_limit_exceeded"
    )


def test_missing_optional_top_level_fields_are_filled_before_strict_validation():
    result = normalize_course_schedule(
        {"document_type": "course_schedule", "courses": [_course()]}
    )

    assert result.semester_label is None
    assert result.institution is None
    assert result.warnings == ()


def test_all_invalid_courses_fail_with_structured_quarantine_report():
    with pytest.raises(CourseScheduleNormalizationError) as captured:
        normalize_course_schedule(_payload([{"weekday": "周五"}]))

    assert captured.value.report.quarantined[0]["path"] == "courses[0]"
