import asyncio
from datetime import datetime, timedelta
import uuid
from zoneinfo import ZoneInfo

from app.agent.context import AgentContext
from app.tools.care import CareTools


TZ = ZoneInfo("Asia/Shanghai")


class RecordingCalendar:
    def __init__(self):
        self.calls = []

    async def get_events(self, participant_id, start_time, end_time):
        self.calls.append((participant_id, start_time, end_time))
        boundary = start_time.isoformat()
        return [{"id": f"event-{len(self.calls)}", "start_time": boundary}]


def _context():
    return AgentContext(uuid.uuid4(), "P", "open", "chat", "message", uuid.uuid4())


def _tools(calendar):
    return CareTools(None, None, calendar, None, "Asia/Shanghai", None)


def _range(days):
    start = datetime(2030, 1, 1, tzinfo=TZ)
    return {
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(days=days)).isoformat(),
    }


def test_calendar_list_31_days_uses_single_window():
    calendar = RecordingCalendar()
    result = asyncio.run(_tools(calendar).list_calendar_events(_context(), _range(31)))

    assert result["ok"] is True
    assert result["window_count"] == 1
    assert len(calendar.calls) == 1


def test_calendar_list_32_days_uses_two_provider_safe_windows():
    calendar = RecordingCalendar()
    result = asyncio.run(_tools(calendar).list_calendar_events(_context(), _range(32)))

    assert result["window_count"] == 2
    assert len(calendar.calls) == 2
    assert all(end - start <= timedelta(days=31) for _, start, end in calendar.calls)


def test_calendar_list_semester_range_is_backend_windowed():
    calendar = RecordingCalendar()
    result = asyncio.run(_tools(calendar).list_calendar_events(_context(), _range(120)))

    assert result["ok"] is True
    assert result["window_count"] == 4
    assert all(end - start <= timedelta(days=31) for _, start, end in calendar.calls)


def test_calendar_list_over_max_range_returns_stable_error_without_provider_call():
    calendar = RecordingCalendar()
    result = asyncio.run(_tools(calendar).list_calendar_events(_context(), _range(181)))

    assert result == {
        "ok": False,
        "error": "calendar_range_too_large",
        "reason_code": "calendar_range_too_large",
        "max_days": 180,
        "retryable": False,
        "do_not_retry": True,
    }
    assert calendar.calls == []


def test_calendar_list_deduplicates_boundary_events():
    class BoundaryCalendar(RecordingCalendar):
        async def get_events(self, participant_id, start_time, end_time):
            self.calls.append((participant_id, start_time, end_time))
            return [
                {"id": "shared", "summary": "边界事件"},
                {"id": f"unique-{len(self.calls)}", "summary": "独立事件"},
            ]

    calendar = BoundaryCalendar()
    result = asyncio.run(_tools(calendar).list_calendar_events(_context(), _range(32)))

    assert [event["id"] for event in result["events"]] == [
        "shared",
        "unique-1",
        "unique-2",
    ]
