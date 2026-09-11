"""Participant-facing timestamp conversion helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def to_participant_local_datetime(
    value: datetime | str | None,
    timezone_name: str,
) -> str | None:
    """Serialize a stored UTC instant in the configured participant timezone."""

    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = str(value).strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo(str(timezone_name))).isoformat()
