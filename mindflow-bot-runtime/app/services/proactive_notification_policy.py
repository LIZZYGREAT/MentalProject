"""Single backend authority for proactive notification eligibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.models import (
    Participant,
    ParticipantCarePreference,
    ProactiveNotificationDelivery,
    utc_now,
)


SYSTEM_PROACTIVE_KINDS = frozenset({
    "warning", "daily_review", "morning_brief", "weekly_summary",
    "care_intervention", "supportive_follow_up",
})
USER_REQUESTED_KINDS = frozenset({"reminder"})
PRIORITIES = {
    "warning": 60,
    "daily_review": 50,
    "morning_brief": 40,
    "care_intervention": 30,
    "supportive_follow_up": 20,
    "weekly_summary": 10,
    "reminder": 100,
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _in_quiet_hours(value: time, start: time | None, end: time | None) -> bool:
    if start is None or end is None or start == end:
        return False
    return start <= value < end if start < end else value >= start or value < end


@dataclass(frozen=True)
class ProactiveDecision:
    allowed: bool
    reason: str
    reservation_id: uuid.UUID | None = None
    quiet_hours_warning: bool = False


class ProactiveNotificationPolicy:
    """Atomically reserve budget and dedupe slots before provider delivery."""

    def __init__(
        self, database: Database, *, timezone_name: str, default_system_budget: int
    ) -> None:
        self.database = database
        self.timezone = ZoneInfo(timezone_name)
        self.default_system_budget = max(0, int(default_system_budget))

    def reserve(
        self,
        participant_id: uuid.UUID,
        *,
        message_kind: str,
        dedupe_key: str,
        scheduled_at: datetime,
        now: datetime | None = None,
    ) -> ProactiveDecision:
        instant = _aware(scheduled_at)
        checked_at = _aware(now or utc_now())
        if message_kind in USER_REQUESTED_KINDS:
            message_class = "user_requested"
        elif message_kind in SYSTEM_PROACTIVE_KINDS:
            message_class = "system_proactive"
        else:
            raise ValueError("unsupported proactive message kind")
        if not str(dedupe_key).strip() or len(str(dedupe_key)) > 160:
            raise ValueError("dedupe_key must contain 1 to 160 characters")

        try:
            with self.database.session() as session:
                # Participant is the lock root even when no preference row
                # exists, so all feature schedulers serialize one budget.
                participant = session.get(
                    Participant, participant_id, with_for_update=True
                )
                if participant is None:
                    return ProactiveDecision(False, "participant_not_found")
                preference = session.get(
                    ParticipantCarePreference, participant_id, with_for_update=True
                )
                reason, quiet_warning = self._eligibility(
                    preference, message_class=message_class,
                    message_kind=message_kind, scheduled_at=instant,
                )
                if reason != "allowed":
                    return ProactiveDecision(False, reason)
                existing = session.execute(
                    select(ProactiveNotificationDelivery).where(
                        ProactiveNotificationDelivery.participant_id
                        == participant_id,
                        ProactiveNotificationDelivery.message_kind
                        == message_kind,
                        ProactiveNotificationDelivery.dedupe_key
                        == str(dedupe_key),
                    ).with_for_update()
                ).scalar_one_or_none()
                if existing is not None and existing.status != "released":
                    return ProactiveDecision(False, "duplicate")
                if message_class == "system_proactive":
                    local_day = instant.astimezone(self.timezone).date()
                    day_start = datetime.combine(local_day, time.min, self.timezone).astimezone(timezone.utc)
                    day_end = day_start + timedelta(days=1)
                    used = session.scalar(
                        select(func.count()).select_from(ProactiveNotificationDelivery).where(
                            ProactiveNotificationDelivery.participant_id == participant_id,
                            ProactiveNotificationDelivery.message_class == "system_proactive",
                            ProactiveNotificationDelivery.scheduled_at >= day_start,
                            ProactiveNotificationDelivery.scheduled_at < day_end,
                            ProactiveNotificationDelivery.status.in_(("reserved", "sent")),
                        )
                    ) or 0
                    configured = (
                        preference.max_system_proactive_per_day
                        if preference is not None else None
                    )
                    budget = self.default_system_budget if configured is None else max(0, int(configured))
                    if used >= budget:
                        return ProactiveDecision(False, "daily_budget")
                if existing is not None:
                    existing.status = "reserved"
                    existing.suppression_reason = None
                    existing.scheduled_at = instant
                    existing.updated_at = checked_at
                    return ProactiveDecision(
                        True, "allowed", existing.id,
                        quiet_hours_warning=quiet_warning,
                    )
                row = ProactiveNotificationDelivery(
                    participant_id=participant_id,
                    message_class=message_class,
                    message_kind=message_kind,
                    priority=PRIORITIES[message_kind],
                    dedupe_key=str(dedupe_key),
                    scheduled_at=instant,
                    status="reserved",
                    created_at=checked_at,
                    updated_at=checked_at,
                )
                session.add(row)
                session.flush()
                return ProactiveDecision(
                    True, "allowed", row.id,
                    quiet_hours_warning=quiet_warning,
                )
        except IntegrityError:
            return ProactiveDecision(False, "duplicate")

    def user_requested_quiet_hours_warning(
        self, participant_id: uuid.UUID, scheduled_at: datetime
    ) -> bool:
        """Inspect creation-time quiet hours without consuming a delivery slot."""

        instant = _aware(scheduled_at)
        with self.database.session() as session:
            preference = session.get(ParticipantCarePreference, participant_id)
            start = preference.quiet_hours_start if preference else None
            end = preference.quiet_hours_end if preference else None
            local_clock = instant.astimezone(self.timezone).time().replace(tzinfo=None)
            return _in_quiet_hours(local_clock, start, end)

    def mark_sent(self, reservation_id: uuid.UUID, *, now: datetime | None = None) -> bool:
        changed_at = _aware(now or utc_now())
        with self.database.session() as session:
            row = session.get(ProactiveNotificationDelivery, reservation_id, with_for_update=True)
            if row is None or row.status not in {"reserved", "sent"}:
                return False
            row.status = "sent"
            row.sent_at = row.sent_at or changed_at
            row.updated_at = changed_at
            return True

    def release(self, reservation_id: uuid.UUID, *, reason: str) -> bool:
        with self.database.session() as session:
            row = session.get(ProactiveNotificationDelivery, reservation_id, with_for_update=True)
            if row is None or row.status != "reserved":
                return False
            row.status = "released"
            row.suppression_reason = str(reason)[:64]
            row.updated_at = utc_now()
            return True

    def _eligibility(
        self,
        preference: ParticipantCarePreference | None,
        *,
        message_class: str,
        message_kind: str,
        scheduled_at: datetime,
    ) -> tuple[str, bool]:
        local_clock = scheduled_at.astimezone(self.timezone).time().replace(tzinfo=None)
        start = preference.quiet_hours_start if preference else None
        end = preference.quiet_hours_end if preference else None
        quiet = _in_quiet_hours(local_clock, start, end)
        if message_class == "user_requested":
            return "allowed", quiet
        if preference is None:
            return "allowed", False
        if not bool(preference.care_enabled):
            return "care_disabled", False
        mute = preference.global_proactive_muted_until or preference.muted_until
        if mute is not None and _aware(mute) > scheduled_at:
            return "global_mute", False
        if quiet:
            return "quiet_hours", False
        feature_switch = {
            "warning": preference.warning_enabled,
            "daily_review": preference.daily_review_enabled,
            "morning_brief": preference.morning_brief_enabled,
            "weekly_summary": preference.weekly_summary_enabled,
            "supportive_follow_up": preference.allow_follow_up,
        }.get(message_kind, True)
        if not bool(feature_switch):
            return "feature_disabled", False
        if (
            message_kind == "morning_brief"
            and preference.morning_brief_paused_until is not None
            and _aware(preference.morning_brief_paused_until) > scheduled_at
        ):
            return "feature_paused", False
        return "allowed", False
