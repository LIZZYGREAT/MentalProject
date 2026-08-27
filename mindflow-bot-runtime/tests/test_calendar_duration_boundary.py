import asyncio
from datetime import datetime, timedelta
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.integrations.feishu.calendar import CalendarService


TZ = ZoneInfo("Asia/Shanghai")


class _ProviderReached(RuntimeError):
    pass


class _Tokens:
    async def get_access_token(self, _participant_id):
        raise _ProviderReached


def _operation(calendar, operation, start, end):
    participant_id = uuid.uuid4()
    if operation == "read":
        return calendar.get_events(participant_id, start, end)
    if operation == "create":
        return calendar.create_event(
            participant_id,
            summary="长事件",
            start_time=start,
            end_time=end,
            source_message_id="duration-boundary",
        )
    return calendar.update_event(
        participant_id,
        "event-id",
        start_time=start,
        end_time=end,
    )


@pytest.mark.parametrize("operation", ["read", "create", "update"])
def test_calendar_range_allows_exactly_31_days(operation):
    calendar = CalendarService(_Tokens())
    start = datetime(2026, 8, 1, 9, 0, tzinfo=TZ)

    # Reaching token resolution proves range validation accepted the boundary;
    # no external HTTP request is made by this test.
    with pytest.raises(_ProviderReached):
        asyncio.run(_operation(calendar, operation, start, start + timedelta(days=31)))


@pytest.mark.parametrize("operation", ["read", "create", "update"])
@pytest.mark.parametrize(
    "excess", [timedelta(microseconds=1), timedelta(hours=1)]
)
def test_calendar_range_rejects_any_duration_over_31_days(operation, excess):
    calendar = CalendarService(_Tokens())
    start = datetime(2026, 8, 1, 9, 0, tzinfo=TZ)

    with pytest.raises(ValueError, match="no longer than 31 days"):
        asyncio.run(
            _operation(
                calendar,
                operation,
                start,
                start + timedelta(days=31) + excess,
            )
        )

