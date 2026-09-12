"""Participant onboarding stage derived from the first real usage time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


STAGE_DAY1 = "day1"
STAGE_WEEK1 = "week1"
STAGE_ACTIVE = "active"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def participant_stage_from_bound_at(
    bound_at: datetime | str | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Classify usage recency from FeishuBinding.bound_at.

    Participant.created_at is deliberately not used: researchers may create
    participants long before the first real Feishu session. Malformed input
    degrades to None instead of failing the agent turn.
    """

    if bound_at is None:
        return None
    if isinstance(bound_at, str):
        normalized = bound_at.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            bound_at = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    if not isinstance(bound_at, datetime):
        return None
    first_used_at = _aware(bound_at)
    current = _aware(now or datetime.now(timezone.utc))
    age = current - first_used_at
    if age < timedelta(hours=24):
        return STAGE_DAY1
    if age < timedelta(days=7):
        return STAGE_WEEK1
    return STAGE_ACTIVE
