from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.forecast_dependency_refresh import (
    ForecastDependencyRefreshService,
    dependent_date_for,
)
from app.services.runtime_clock import RuntimeClock


LOCAL_TODAY = date(2026, 9, 14)


@pytest.mark.parametrize(
    ("source_date", "expected"),
    [
        (LOCAL_TODAY - timedelta(days=1), LOCAL_TODAY),
        (LOCAL_TODAY, LOCAL_TODAY + timedelta(days=1)),
        (LOCAL_TODAY - timedelta(days=2), None),
        (LOCAL_TODAY + timedelta(days=1), None),
    ],
)
def test_dependent_date_for_has_bounded_contract(source_date, expected):
    assert dependent_date_for(source_date, LOCAL_TODAY) == expected


def test_dependency_service_uses_injected_clock():
    clock = RuntimeClock(
        "Asia/Shanghai",
        now_fn=lambda: datetime(
            2026, 9, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
    )
    service = ForecastDependencyRefreshService(
        object(), object(), object(), timezone_name="Asia/Shanghai", clock=clock
    )

    assert service.dependent_date(date(2026, 9, 13)) == date(2026, 9, 14)
    assert service.dependent_date(date(2026, 9, 14)) == date(2026, 9, 15)
    assert service.dependent_date(date(2026, 9, 12)) is None
    assert service.dependent_date(date(2026, 9, 15)) is None


def test_dependency_service_can_reuse_a_captured_reference_date():
    service = ForecastDependencyRefreshService(
        object(),
        object(),
        object(),
        timezone_name="Asia/Shanghai",
        clock=RuntimeClock(
            "Asia/Shanghai",
            now_fn=lambda: datetime(
                2026, 9, 15, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai")
            ),
        ),
    )

    assert service.dependent_date(
        date(2026, 9, 14), reference_local_date=date(2026, 9, 14)
    ) == date(2026, 9, 15)
