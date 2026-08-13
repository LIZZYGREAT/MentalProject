from app.agent.skill_loader import SkillLoader
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from algorithm.time_utils import extract_hhmm, interval_minutes, parse_datetime_on_date
from mindflow_core.assessment import AssessmentModel, normalize_event_datetime
from helpers import skill_path


def test_skill_frontmatter_is_parsed_and_stable():
    loader = SkillLoader(skill_path())
    first = loader.load()
    second = loader.current()
    assert first.metadata["name"] == "mental-health-care"
    assert "care_record_checkin" in first.instructions
    assert first.version == second.version


def test_existing_algorithm_runs_through_structured_adapter():
    result = AssessmentModel("Asia/Shanghai").predict(
        profile={},
        observations=[
            {
                "type": "checkin",
                "observed_at": "2026-08-09T08:00:00+08:00",
                "payload": {"stress_0_10": 4, "energy_0_10": 7},
            }
        ],
        calendar_events=[],
        local_date="2026-08-09",
        calendar_degraded=True,
    )
    assert 0 <= result.stress_0_10 <= 10
    assert 0 <= result.vitality_0_10 <= 10
    assert result.point_count > 0
    assert result.calendar_degraded is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2030-01-15T07:00:00+00:00", datetime(2030, 1, 15, 15, 0)),
        ("2030-01-15T15:00:00+08:00", datetime(2030, 1, 15, 15, 0)),
        ("2030-01-15T07:00:00Z", datetime(2030, 1, 15, 15, 0)),
        ("2030-01-15T23:30:00+00:00", datetime(2030, 1, 16, 7, 30)),
        ("15:00", datetime(2030, 1, 15, 15, 0)),
        ("2030-01-15 15:00:00", datetime(2030, 1, 15, 15, 0)),
    ],
)
def test_normalize_event_datetime_supports_calendar_and_legacy_contracts(
    value, expected
):
    assert normalize_event_datetime(
        value, "2030-01-15", ZoneInfo("Asia/Shanghai")
    ) == expected


def test_time_utils_defensively_parse_iso_without_guessing_timezone():
    value = "2030-01-15T15:00:00+08:00"
    assert extract_hhmm(value) == "15:00"
    assert parse_datetime_on_date(value, "2030-01-15") == datetime(
        2030, 1, 15, 15, 0
    )
    assert interval_minutes(
        "2030-01-15T15:30:00+08:00",
        "2030-01-15T17:00:00+08:00",
    ) == 90.0


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2030-01-15T07:00:00+00:00", "2030-01-15T08:00:00+00:00"),
        ("2030-01-15T15:00:00+08:00", "2030-01-15T16:00:00+08:00"),
        ("2030-01-15T07:00:00Z", "2030-01-15T08:00:00Z"),
        ("15:00", "16:00"),
        ("2030-01-15 15:00:00", "2030-01-15 16:00:00"),
    ],
)
def test_real_assessment_model_accepts_iso_and_legacy_event_times(start, end):
    result = AssessmentModel("Asia/Shanghai").predict(
        profile={}, observations=[], local_date="2030-01-15",
        calendar_degraded=False,
        calendar_events=[{
            "id": "event-1", "summary": "准备汇报", "event_type": "task",
            "start_time": start, "end_time": end,
        }],
    )
    assert result.point_count > 0
    assert any(
        "准备汇报" in point["current_events"]
        for point in result.trajectory
        if point["time"] == "15:00"
    )


def test_real_assessment_model_preserves_local_midnight_crossing():
    timezone = ZoneInfo("Asia/Shanghai")
    start = normalize_event_datetime(
        "2030-01-15T15:30:00+00:00", "2030-01-15", timezone
    )
    end = normalize_event_datetime(
        "2030-01-15T17:00:00+00:00", "2030-01-15", timezone
    )
    assert start == datetime(2030, 1, 15, 23, 30)
    assert end == datetime(2030, 1, 16, 1, 0)
    assert (end - start).total_seconds() / 60 == 90

    result = AssessmentModel("Asia/Shanghai").predict(
        profile={}, observations=[], local_date="2030-01-15",
        calendar_degraded=False,
        calendar_events=[{
            "id": "overnight", "summary": "深夜项目", "event_type": "task",
            "start_time": "2030-01-15T15:30:00+00:00",
            "end_time": "2030-01-15T17:00:00+00:00",
        }],
    )
    assert any(
        "深夜项目" in point["current_events"]
        for point in result.trajectory
        if point["time"] == "23:30"
    )


def test_real_assessment_model_handles_multiple_utc_calendar_events():
    events = []
    for index, (start, end) in enumerate(
        [
            ("01:30", "03:00"),
            ("04:30", "06:30"),
            ("07:00", "09:30"),
            ("11:00", "13:30"),
        ]
    ):
        events.append({
            "id": f"event-{index}", "summary": f"日程 {index}",
            "event_type": "task",
            "start_time": f"2030-01-15T{start}:00+00:00",
            "end_time": f"2030-01-15T{end}:00+00:00",
        })
    result = AssessmentModel("Asia/Shanghai").predict(
        profile={}, observations=[], calendar_events=events,
        local_date="2030-01-15", calendar_degraded=False,
    )
    assert result.calendar_event_count == 4
    assert result.point_count > 0
