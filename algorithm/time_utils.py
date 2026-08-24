"""Shared time parsing and interval helpers for event and schedule logic."""

from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Optional, Tuple

from settings.model_defaults import (
    DEFAULT_DATE_FORMAT,
    DEFAULT_TIME_FORMAT,
    MIN_EVENT_DURATION_MINUTES,
)


def _parse_full_datetime(value: Any) -> Optional[datetime]:
    """Parse supported date-bearing values without assuming a timezone."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if len(text) < 10 or text[4:5] != "-" or text[7:8] != "-":
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def normalize_observation_to_model_step(
    value: Any,
    *,
    step_minutes: int,
    target_date: date | str,
    timezone_value: tzinfo | None = None,
) -> Optional[datetime]:
    """Ceil one date-bearing observation to a causal model-grid timestamp.

    The observation must belong to ``target_date`` before rounding and must
    remain on that date afterwards.  A late observation such as 23:58 on a
    five-minute grid therefore does not get moved backwards to 23:55 or
    leaked into the following day's simulation.
    """

    parsed = _parse_full_datetime(value)
    if parsed is None:
        return None
    if timezone_value is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone_value)
        else:
            parsed = parsed.astimezone(timezone_value)
    requested_date = (
        date.fromisoformat(target_date)
        if isinstance(target_date, str)
        else target_date
    )
    if parsed.date() != requested_date:
        return None
    step = int(step_minutes)
    if step <= 0:
        raise ValueError("step_minutes must be positive")

    midnight = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = parsed - midnight
    elapsed_microseconds = (
        (elapsed.days * 24 * 60 * 60 + elapsed.seconds) * 1_000_000
        + elapsed.microseconds
    )
    step_microseconds = step * 60 * 1_000_000
    remainder = elapsed_microseconds % step_microseconds
    if remainder:
        elapsed_microseconds += step_microseconds - remainder
    aligned = midnight + timedelta(microseconds=elapsed_microseconds)
    if aligned.date() != requested_date:
        return None
    return aligned


def extract_hhmm(value: Any, fallback: str = "00:00") -> str:
    """Extract an ``HH:MM`` string from a datetime-like value."""
    if isinstance(value, datetime):
        return value.strftime(DEFAULT_TIME_FORMAT)
    if value is None:
        return fallback

    text = str(value).strip()
    if not text:
        return fallback
    parsed = _parse_full_datetime(text)
    if parsed is not None:
        return parsed.strftime(DEFAULT_TIME_FORMAT)
    text = text.split(" ")[-1]
    parts = text.split(":")
    if len(parts) >= 2:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    return fallback


def time_to_minutes(value: Any, fallback: str = "00:00") -> int:
    """Convert a time value to minutes from midnight."""
    hhmm = extract_hhmm(value, fallback=fallback)
    hour, minute = map(int, hhmm.split(":"))
    return hour * 60 + minute


def minutes_to_hhmm(minutes: int) -> str:
    """Convert minutes from midnight to a normalized ``HH:MM`` string."""
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_datetime_on_date(value: Any, date_str: str, fallback: str = "00:00") -> datetime:
    """Parse a datetime or time string on the requested simulation date."""
    if isinstance(value, datetime):
        base = datetime.strptime(date_str, DEFAULT_DATE_FORMAT)
        return value.replace(year=base.year, month=base.month, day=base.day)
    parsed = _parse_full_datetime(value)
    if parsed is not None:
        # Business timezone conversion belongs to the assessment adapter. This
        # defensive path only preserves the supplied wall-clock representation.
        return parsed.replace(tzinfo=None)
    hhmm = extract_hhmm(value, fallback=fallback)
    return datetime.strptime(f"{date_str} {hhmm}", f"{DEFAULT_DATE_FORMAT} {DEFAULT_TIME_FORMAT}")


def interval_minutes(start: Any, end: Any, default: float = 30.0) -> float:
    """Return duration in minutes, treating negative spans as crossing midnight."""
    try:
        parsed_start = _parse_full_datetime(start)
        parsed_end = _parse_full_datetime(end)
        if parsed_start is not None and parsed_end is not None:
            minutes = (parsed_end - parsed_start).total_seconds() / 60.0
        else:
            minutes = time_to_minutes(end) - time_to_minutes(start)
        if minutes < 0:
            minutes += 24 * 60
        return max(MIN_EVENT_DURATION_MINUTES, float(minutes))
    except Exception:
        return max(MIN_EVENT_DURATION_MINUTES, float(default))


def elapsed_minutes(start: Any, current_time: datetime, default: float = 0.0) -> float:
    """Return minutes elapsed from event start to current time on the same day."""
    try:
        if isinstance(start, datetime):
            start_dt = start.replace(
                year=current_time.year,
                month=current_time.month,
                day=current_time.day,
            )
        else:
            start_dt = datetime.strptime(
                f"{current_time.strftime(DEFAULT_DATE_FORMAT)} {extract_hhmm(start)}",
                f"{DEFAULT_DATE_FORMAT} {DEFAULT_TIME_FORMAT}",
            )
        minutes = (current_time - start_dt).total_seconds() / 60.0
        if minutes < 0:
            minutes += 24 * 60
        return max(0.0, minutes)
    except Exception:
        return default


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """Return whether two same-day time intervals overlap."""
    return max(a_start, b_start) < min(a_end, b_end)


def normalize_interval(start: Any, end: Any) -> Tuple[str, str]:
    """Return normalized start and end ``HH:MM`` strings."""
    return extract_hhmm(start), extract_hhmm(end)
