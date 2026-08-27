from datetime import date, datetime, timedelta
import time
from zoneinfo import ZoneInfo

import pytest

from app.services.calendar_mutation_impact import CalendarMutationImpactResolver


TZ = ZoneInfo("Asia/Shanghai")


def _event(start, rule):
    return {
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
        "recurrence": rule,
    }


def test_large_yearly_count_matches_target_without_expanding_future_dates():
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")
    start = datetime(2030, 1, 1, 9, 0, tzinfo=TZ)
    target = date(2129, 1, 1)

    started = time.perf_counter()
    affected = resolver.affected_dates(
        previous=_event(start, "FREQ=YEARLY;INTERVAL=99;COUNT=100"),
        updated=None,
        persisted_dates={target},
    )
    elapsed = time.perf_counter() - started

    assert target in affected
    assert elapsed < 0.1


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;INTERVAL=99;COUNT=999",
        "FREQ=WEEKLY;INTERVAL=99;BYDAY=FR;COUNT=999",
        "FREQ=MONTHLY;INTERVAL=99;COUNT=999",
        "FREQ=YEARLY;INTERVAL=99;COUNT=999",
    ],
)
def test_counted_recurrence_near_date_max_never_overflows(rule):
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")
    start = datetime(9900, 1, 1, 9, 0, tzinfo=TZ)

    affected = resolver.affected_dates(
        previous=_event(start, rule),
        updated=None,
        persisted_dates={date.max},
    )

    assert isinstance(affected, set)


def test_yearly_february_29_count_uses_only_real_occurrences():
    resolver = CalendarMutationImpactResolver("Asia/Shanghai")
    start = datetime(2028, 2, 29, 9, 0, tzinfo=TZ)
    event = _event(start, "FREQ=YEARLY;INTERVAL=1;COUNT=3")

    affected = resolver.affected_dates(
        previous=event,
        updated=None,
        persisted_dates={
            date(2032, 2, 29),
            date(2036, 2, 29),
            date(2040, 2, 29),
        },
    )

    assert date(2032, 2, 29) in affected
    assert date(2036, 2, 29) in affected
    assert date(2040, 2, 29) not in affected

