import asyncio
from datetime import date, datetime, timedelta
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.services.course_series_resolver import (
    CourseSeriesResolutionError,
    CourseSeriesResolver,
)


TZ = ZoneInfo("Asia/Shanghai")
OWNER = uuid.uuid4()


def _event(
    event_id: str,
    day: date,
    *,
    title: str = "操作系统(0955)",
    start: str = "12:55",
    end: str = "14:30",
    parent: str = "",
    location: str = "津南公教楼B区102",
    recurrence: str = "",
) -> dict:
    start_time = datetime.combine(day, datetime.strptime(start, "%H:%M").time(), TZ)
    end_time = datetime.combine(day, datetime.strptime(end, "%H:%M").time(), TZ)
    return {
        "id": event_id,
        "summary": title,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "recurring_event_id": parent,
        "location": location,
        "recurrence": recurrence,
    }


class _Calendar:
    def __init__(self, events: list[dict], masters: dict[str, dict] | None = None):
        self.events = events
        self.masters = masters or {}
        self.windows: list[tuple[datetime, datetime]] = []

    async def get_events(self, _participant_id, start_time, end_time):
        self.windows.append((start_time, end_time))
        return [
            item
            for item in self.events
            if start_time
            <= datetime.fromisoformat(item["start_time"])
            < end_time
        ]

    async def get_event(self, _participant_id, event_id):
        return self.masters[event_id]


class _Imports:
    def __init__(self, provenance: dict | None):
        self.provenance = provenance

    def resolve_calendar_event_provenance(self, _participant_id, event_id):
        if not self.provenance:
            return None
        return self.provenance if event_id in {"occ-3", "master-theory"} else None


def test_import_provenance_resolves_only_exact_course_item_and_windows_range():
    semester_start = date(2026, 9, 7)
    theory = [
        _event(f"occ-{week}", semester_start + timedelta(weeks=week - 1, days=1), parent="master-theory")
        for week in range(1, 18)
    ]
    lab = [
        _event(
            f"lab-{week}",
            semester_start + timedelta(weeks=week - 1, days=1),
            title="操作系统实验(0955)",
            start="16:00",
            end="17:35",
            parent="master-lab",
            location="实验楼201",
        )
        for week in range(1, 18)
    ]
    calendar = _Calendar(theory + lab)
    imports = _Imports(
        {
            "import_id": "import-1",
            "course_item_id": "item-theory",
            "display_name": "操作系统(0955)",
            "semester_start_date": semester_start.isoformat(),
            "weekday": 2,
            "end_time": "14:30",
            "week_rule": {"start_week": 1, "end_week": 17, "odd_even": "all"},
            "provider_event_ids": ("master-theory",),
        }
    )
    resolver = CourseSeriesResolver(
        calendar, timezone_name="Asia/Shanghai", course_imports=imports
    )

    resolved = asyncio.run(
        resolver.resolve(
            OWNER,
            anchor_event=theory[2],
            scope="current_semester_remainder",
            reference_local_date=date(2026, 9, 22),
        )
    )

    assert resolved.resolution_source == "course_import"
    assert resolved.course_identity.endswith(":item-theory")
    assert resolved.occurrence_event_ids == tuple(f"occ-{week}" for week in range(3, 18))
    assert all(end - start <= timedelta(days=31) for start, end in calendar.windows)
    assert len(calendar.windows) >= 4


def test_provider_series_uses_count_boundary_and_not_title_matching():
    first = datetime(2026, 9, 8, 12, 55, tzinfo=TZ)
    master = _event(
        "master-theory",
        first.date(),
        recurrence="FREQ=WEEKLY;INTERVAL=1;COUNT=4;BYDAY=TU",
    )
    instances = [
        _event(
            f"occ-{index}",
            (first + timedelta(weeks=index - 1)).date(),
            parent="master-theory",
        )
        for index in range(1, 5)
    ]
    same_title_other_series = _event(
        "other-3", date(2026, 9, 22), parent="master-other"
    )
    calendar = _Calendar(
        instances + [same_title_other_series], {"master-theory": master}
    )
    resolver = CourseSeriesResolver(calendar, timezone_name="Asia/Shanghai")

    resolved = asyncio.run(
        resolver.resolve(
            OWNER,
            anchor_event=instances[1],
            scope="current_semester_remainder",
            reference_local_date=date(2026, 9, 15),
        )
    )

    assert resolved.occurrence_event_ids == ("occ-2", "occ-3", "occ-4")
    assert resolved.resolution_source == "provider_series"


def test_series_without_backend_identity_or_recurrence_is_ambiguous():
    anchor = _event("one-off", date(2026, 9, 22), parent="")
    resolver = CourseSeriesResolver(_Calendar([anchor]), timezone_name="Asia/Shanghai")

    with pytest.raises(CourseSeriesResolutionError) as exc_info:
        asyncio.run(
            resolver.resolve(
                OWNER,
                anchor_event=anchor,
                scope="current_semester_remainder",
                reference_local_date=date(2026, 9, 22),
            )
        )

    assert exc_info.value.code == "course_series_ambiguous"


def test_single_occurrence_never_expands_or_queries_calendar():
    anchor = _event("one-off", date(2026, 9, 22))
    calendar = _Calendar([anchor])
    resolver = CourseSeriesResolver(calendar, timezone_name="Asia/Shanghai")

    resolved = asyncio.run(
        resolver.resolve(
            OWNER,
            anchor_event=anchor,
            scope="single_occurrence",
            reference_local_date=date(2026, 9, 22),
        )
    )

    assert resolved.occurrence_event_ids == ("one-off",)
    assert calendar.windows == []
