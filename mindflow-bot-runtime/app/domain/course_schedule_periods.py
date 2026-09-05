"""Deterministic school-period resolution for course schedule imports."""

from __future__ import annotations

from datetime import time
from typing import Mapping


DEFAULT_PERIOD_MAP_VERSION = "school_schedule_v1"

DEFAULT_SCHOOL_PERIODS: dict[int, tuple[time, time]] = {
    1: (time(8, 0), time(8, 45)),
    2: (time(8, 55), time(9, 40)),
    3: (time(10, 0), time(10, 45)),
    4: (time(10, 55), time(11, 40)),
    5: (time(12, 0), time(12, 45)),
    6: (time(12, 55), time(13, 40)),
    7: (time(14, 0), time(14, 45)),
    8: (time(14, 55), time(15, 40)),
    9: (time(16, 0), time(16, 45)),
    10: (time(16, 55), time(17, 40)),
    11: (time(18, 30), time(19, 15)),
    12: (time(19, 25), time(20, 10)),
    13: (time(20, 30), time(21, 15)),
    14: (time(21, 25), time(22, 10)),
}


def resolve_period_time(
    period_start: int | None,
    period_end: int | None,
    *,
    actual_start: time | None = None,
    actual_end: time | None = None,
    period_mapping: Mapping[int, tuple[time, time]] | None = None,
    range_overrides: Mapping[tuple[int, int], tuple[time, time]] | None = None,
) -> tuple[time, time, str] | None:
    """Resolve image time > user mapping > versioned school defaults."""

    if actual_start is not None or actual_end is not None:
        if actual_start is None or actual_end is None or actual_end <= actual_start:
            raise ValueError("actual course time range is invalid")
        return actual_start, actual_end, "image"
    if period_start is None or period_end is None or period_end < period_start:
        return None

    exact = dict(range_overrides or {}).get((period_start, period_end))
    if exact is not None:
        start, end = exact
        if end <= start:
            raise ValueError("period range override is invalid")
        return start, end, "user"

    user = dict(period_mapping or {})
    start_period = user.get(period_start) or DEFAULT_SCHOOL_PERIODS.get(period_start)
    end_period = user.get(period_end) or DEFAULT_SCHOOL_PERIODS.get(period_end)
    if start_period is None or end_period is None:
        return None
    start, end = start_period[0], end_period[1]
    if end <= start:
        raise ValueError("resolved course period range is invalid")
    source = "user" if period_start in user or period_end in user else "default"
    return start, end, source


def split_period_mapping(
    mapping: Mapping[int | tuple[int, int], tuple[time, time]],
) -> tuple[
    dict[int, tuple[time, time]],
    dict[tuple[int, int], tuple[time, time]],
]:
    """Normalize user-provided single-period and explicit range overrides."""

    singles: dict[int, tuple[time, time]] = {}
    ranges: dict[tuple[int, int], tuple[time, time]] = {}
    for raw_key, clocks in mapping.items():
        start, end = clocks
        if end <= start:
            raise ValueError("period end time must be after start time")
        if isinstance(raw_key, int):
            if raw_key < 1 or raw_key > 30:
                raise ValueError("period number is out of range")
            singles[raw_key] = (start, end)
            continue
        if (
            not isinstance(raw_key, tuple)
            or len(raw_key) != 2
            or not all(isinstance(value, int) for value in raw_key)
        ):
            raise ValueError("period mapping key is invalid")
        first, last = raw_key
        if first < 1 or last < first or last > 30:
            raise ValueError("period range is out of range")
        ranges[(first, last)] = (start, end)
    return singles, ranges
