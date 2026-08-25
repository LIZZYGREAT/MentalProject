"""Durable care preferences, interventions, user actions, and feedback."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import uuid
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.models import (
    CareInterventionEvent,
    CareInterventionFeedback,
    DailyReviewSchedule,
    Participant,
    ParticipantCarePreference,
    WarningSchedule,
    utc_now,
)


CARE_ACTIONS = {
    "ack",
    "snooze_30",
    "mute_today",
    "helpful",
    "not_relevant",
    "too_early",
    "too_late",
}
CARE_CARD_ACTIONS = ["ack", "snooze_30", "mute_today", "helpful", "not_relevant"]
OPERATIONAL_CARE_ACTIONS = {"ack", "snooze_30", "mute_today"}
PREFERRED_SUPPORT_TYPES = {
    "micro_break",
    "hydration",
    "walk",
    "task_decomposition",
    "transition_buffer",
    "recovery",
    "trusted_person",
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _parse_clock(value: Any) -> time | None:
    if value is None or value == "":
        return None
    try:
        parsed = time.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError("quiet hours must use HH:MM") from exc
    return parsed.replace(second=0, microsecond=0)


def _in_quiet_hours(value: time, start: time | None, end: time | None) -> bool:
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= value < end
    return value >= start or value < end


class ParticipantCarePreferenceRepository:
    def __init__(
        self,
        database: Database,
        *,
        system_max_daily_sends: int,
        timezone_name: str,
    ):
        self.database = database
        self.system_max_daily_sends = max(0, int(system_max_daily_sends))
        self.timezone = ZoneInfo(timezone_name)

    def defaults(self) -> dict[str, Any]:
        return {
            "care_enabled": True,
            "warning_enabled": True,
            "daily_review_enabled": True,
            "morning_brief_enabled": False,
            "weekly_summary_enabled": False,
            "quiet_hours_start": None,
            "quiet_hours_end": None,
            "max_proactive_care_per_day": self.system_max_daily_sends,
            "effective_max_proactive_care_per_day": self.system_max_daily_sends,
            "allow_schedule_suggestions": False,
            "allow_follow_up": True,
            "preferred_support_types": [],
            "muted_until": None,
            "version": 0,
            "updated_at": None,
        }

    def get(self, participant_id: uuid.UUID) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(ParticipantCarePreference, participant_id)
            return self._view(row) if row is not None else self.defaults()

    def update(
        self,
        participant_id: uuid.UUID,
        changes: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        changed_at = _aware(now or utc_now())
        allowed = {
            "care_enabled",
            "warning_enabled",
            "daily_review_enabled",
            "morning_brief_enabled",
            "weekly_summary_enabled",
            "quiet_hours_start",
            "quiet_hours_end",
            "max_proactive_care_per_day",
            "allow_schedule_suggestions",
            "allow_follow_up",
            "preferred_support_types",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported care preference fields: {sorted(unknown)}")
        with self.database.session() as session:
            participant = session.get(
                Participant, participant_id, with_for_update=True
            )
            if participant is None:
                raise ValueError("participant does not exist")
            row = session.get(
                ParticipantCarePreference, participant_id, with_for_update=True
            )
            created_preference = row is None
            if row is None:
                row = ParticipantCarePreference(
                    participant_id=participant_id,
                    version=0,
                )
                session.add(row)
                session.flush()
            before = self._view(row)
            self._apply_changes(row, changes)
            after_values = self._view(row)
            material_keys = allowed
            if (
                (created_preference and bool(changes))
                or any(
                    before.get(key) != after_values.get(key)
                    for key in material_keys
                )
            ):
                row.version = int(row.version or 0) + 1
                row.updated_at = changed_at
            self._cancel_disallowed_in_session(
                session, participant_id, row, now=changed_at
            )
            session.flush()
            return self._view(row)

    def mute_today(
        self,
        participant_id: uuid.UUID,
        local_date: date,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        changed_at = _aware(now or utc_now())
        local_end = datetime.combine(
            local_date + timedelta(days=1), time.min, self.timezone
        ).astimezone(timezone.utc)
        with self.database.session() as session:
            participant = session.get(
                Participant, participant_id, with_for_update=True
            )
            if participant is None:
                raise ValueError("participant does not exist")
            row = session.get(
                ParticipantCarePreference, participant_id, with_for_update=True
            )
            if row is None:
                row = ParticipantCarePreference(
                    participant_id=participant_id,
                    version=0,
                )
                session.add(row)
                session.flush()
            current = _aware(row.muted_until) if row.muted_until else None
            if current is None or current < local_end:
                row.muted_until = local_end
                row.version = int(row.version or 0) + 1
                row.updated_at = changed_at
            self._cancel_disallowed_in_session(
                session, participant_id, row, now=changed_at
            )
            session.flush()
            return self._view(row)

    def allows_scheduled_at(
        self, preferences: Mapping[str, Any], scheduled_at: datetime
    ) -> bool:
        return self._allows_proactive_at(
            preferences,
            scheduled_at,
            feature_key="warning_enabled",
        )

    def allows_daily_review_at(
        self, preferences: Mapping[str, Any], scheduled_at: datetime
    ) -> bool:
        return self._allows_proactive_at(
            preferences,
            scheduled_at,
            feature_key="daily_review_enabled",
        )

    def _allows_proactive_at(
        self,
        preferences: Mapping[str, Any],
        scheduled_at: datetime,
        *,
        feature_key: str,
    ) -> bool:
        instant = _aware(scheduled_at)
        if not bool(preferences.get("care_enabled", True)):
            return False
        if not bool(preferences.get(feature_key, True)):
            return False
        muted_until = preferences.get("muted_until")
        if muted_until:
            try:
                parsed = datetime.fromisoformat(str(muted_until))
            except ValueError:
                parsed = None
            if parsed is not None and _aware(parsed) > instant:
                return False
        local_clock = instant.astimezone(self.timezone).time().replace(tzinfo=None)
        start = _parse_clock(preferences.get("quiet_hours_start"))
        end = _parse_clock(preferences.get("quiet_hours_end"))
        return not _in_quiet_hours(local_clock, start, end)

    def _apply_changes(
        self, row: ParticipantCarePreference, changes: Mapping[str, Any]
    ) -> None:
        for key in (
            "care_enabled",
            "warning_enabled",
            "daily_review_enabled",
            "morning_brief_enabled",
            "weekly_summary_enabled",
            "allow_schedule_suggestions",
            "allow_follow_up",
        ):
            if key in changes:
                value = changes[key]
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be boolean")
                setattr(row, key, value)
        if "max_proactive_care_per_day" in changes:
            raw_max = changes["max_proactive_care_per_day"]
            if raw_max is None:
                row.max_proactive_care_per_day = None
            elif (
                not isinstance(raw_max, int)
                or isinstance(raw_max, bool)
                or not 0 <= raw_max <= self.system_max_daily_sends
            ):
                raise ValueError(
                    "max_proactive_care_per_day exceeds the backend safety cap"
                )
            else:
                row.max_proactive_care_per_day = raw_max
        start = (
            _parse_clock(changes["quiet_hours_start"])
            if "quiet_hours_start" in changes
            else row.quiet_hours_start
        )
        end = (
            _parse_clock(changes["quiet_hours_end"])
            if "quiet_hours_end" in changes
            else row.quiet_hours_end
        )
        if (start is None) != (end is None):
            raise ValueError("quiet_hours_start and quiet_hours_end must be set together")
        row.quiet_hours_start = start
        row.quiet_hours_end = end
        if "preferred_support_types" in changes:
            raw_types = changes["preferred_support_types"]
            if not isinstance(raw_types, (list, tuple)):
                raise ValueError("preferred_support_types must be a list")
            normalized = sorted({str(value) for value in raw_types})
            if not set(normalized) <= PREFERRED_SUPPORT_TYPES:
                raise ValueError("preferred_support_types contains unsupported values")
            row.preferred_support_types = normalized

    def _cancel_disallowed_in_session(
        self,
        session: Any,
        participant_id: uuid.UUID,
        preference: ParticipantCarePreference,
        *,
        now: datetime,
    ) -> None:
        view = self._view(preference)
        rows = session.execute(
            select(WarningSchedule).where(
                WarningSchedule.participant_id == participant_id,
                WarningSchedule.status.in_(("pending", "claimed", "delivery_unavailable")),
            ).with_for_update()
        ).scalars().all()
        allowed_count = int(view["effective_max_proactive_care_per_day"])
        kept_by_date: dict[date, int] = {}
        sent_by_date: dict[date, int] = {}
        for warning in sorted(rows, key=lambda item: item.target_time):
            if (
                warning.status == "claimed"
                and warning.authorized_at is not None
                and warning.lease_until is not None
                and _aware(warning.lease_until) >= now
            ):
                # Authorization is the commit point for an in-flight provider
                # request. Preference changes apply to later deliveries.
                continue
            if warning.local_date not in sent_by_date:
                sent_by_date[warning.local_date] = len(
                    session.execute(
                        select(WarningSchedule.id).where(
                            WarningSchedule.participant_id == participant_id,
                            WarningSchedule.local_date == warning.local_date,
                            WarningSchedule.status.in_(("sent", "escalated")),
                        )
                    ).all()
                )
            remaining = max(0, allowed_count - sent_by_date[warning.local_date])
            kept = kept_by_date.get(warning.local_date, 0)
            permitted = (
                kept < remaining
                and self.allows_scheduled_at(view, warning.target_time)
            )
            if permitted:
                kept_by_date[warning.local_date] = kept + 1
                continue
            warning.status = "cancelled"
            warning.claim_token = None
            warning.claimed_at = None
            warning.lease_until = None
            warning.authorized_at = None
            warning.next_attempt_at = None
            warning.payload_json = {
                **dict(warning.payload_json),
                "cancellation_reason": "participant_care_preference",
            }
            warning.updated_at = now
            CareInterventionRepository.mirror_warning_in_session(session, warning)

        daily_reviews = session.execute(
            select(DailyReviewSchedule).where(
                DailyReviewSchedule.participant_id == participant_id,
                DailyReviewSchedule.status.in_(
                    ("pending", "claimed", "delivery_unavailable")
                ),
            ).with_for_update()
        ).scalars().all()
        for review in daily_reviews:
            if (
                review.status == "claimed"
                and review.authorized_at is not None
                and review.lease_until is not None
                and _aware(review.lease_until) >= now
            ):
                continue
            if self.allows_daily_review_at(view, review.scheduled_at):
                continue
            review.status = "cancelled"
            review.claim_token = None
            review.claimed_at = None
            review.lease_until = None
            review.authorized_at = None
            review.next_attempt_at = None
            review.last_error_code = "participant_care_preference"
            review.last_error_class = None
            review.updated_at = now

    def _view(self, row: ParticipantCarePreference) -> dict[str, Any]:
        configured_max = row.max_proactive_care_per_day
        effective_max = min(
            self.system_max_daily_sends,
            self.system_max_daily_sends if configured_max is None else configured_max,
        )
        return {
            "care_enabled": bool(row.care_enabled),
            "warning_enabled": bool(row.warning_enabled),
            "daily_review_enabled": bool(row.daily_review_enabled),
            "morning_brief_enabled": bool(row.morning_brief_enabled),
            "weekly_summary_enabled": bool(row.weekly_summary_enabled),
            "quiet_hours_start": (
                row.quiet_hours_start.strftime("%H:%M")
                if row.quiet_hours_start else None
            ),
            "quiet_hours_end": (
                row.quiet_hours_end.strftime("%H:%M")
                if row.quiet_hours_end else None
            ),
            "max_proactive_care_per_day": (
                configured_max
                if configured_max is not None
                else self.system_max_daily_sends
            ),
            "effective_max_proactive_care_per_day": effective_max,
            "allow_schedule_suggestions": bool(row.allow_schedule_suggestions),
            "allow_follow_up": bool(row.allow_follow_up),
            "preferred_support_types": list(row.preferred_support_types or []),
            "muted_until": (
                _aware(row.muted_until).isoformat() if row.muted_until else None
            ),
            "version": int(row.version),
            "updated_at": _aware(row.updated_at).isoformat(),
        }


class CareInterventionRepository:
    def __init__(
        self,
        database: Database,
        preferences: ParticipantCarePreferenceRepository,
    ):
        self.database = database
        self.preferences = preferences

    @staticmethod
    def mirror_warning_in_session(session: Any, warning: WarningSchedule) -> None:
        payload = dict(warning.payload_json or {})
        plan = dict(payload.get("care_plan") or {})
        provenance = dict(payload.get("care_provenance") or {})
        context = dict(payload.get("care_context") or {})
        row = session.execute(
            select(CareInterventionEvent).where(
                CareInterventionEvent.source_warning_id == warning.id
            ).with_for_update()
        ).scalar_one_or_none()
        delivery_status = str(warning.status)
        status = (
            "sent" if delivery_status in {"sent", "escalated"}
            else delivery_status
        )
        values = {
            "participant_id": warning.participant_id,
            "source_forecast_id": warning.forecast_id,
            "forecast_version": warning.forecast_version,
            "intervention_type": str(
                plan.get("intervention_type") or "generic_fallback"
            )[:64],
            "template_id": str(
                provenance.get("template_id") or plan.get("template_id") or "legacy-fallback"
            )[:128],
            "template_version": str(
                provenance.get("template_version") or "1.0.0"
            )[:32],
            "reason_code": str(
                plan.get("reason_code") or "forecast_warning"
            )[:128],
            "scheduled_at": warning.target_time,
            "sent_at": warning.sent_at,
            "delivery_status": delivery_status,
            "message_text": str(
                payload.get("message") or payload.get("fallback_message") or ""
            )[:4000],
            "context_json": {
                "care_context": context,
                "care_plan": plan,
                "care_provenance": provenance,
            },
            "actions_json": list(plan.get("actions") or CARE_CARD_ACTIONS),
            "updated_at": warning.updated_at,
        }
        if row is None:
            session.add(
                CareInterventionEvent(
                    id=warning.id,
                    source_warning_id=warning.id,
                    status=status,
                    created_at=warning.updated_at,
                    **values,
                )
            )
            return
        for key, value in values.items():
            if key == "updated_at" and row.user_action and row.updated_at:
                value = max(_aware(row.updated_at), _aware(value))
            setattr(row, key, value)
        if not row.user_action:
            row.status = status

    def latest_sent(self, participant_id: uuid.UUID) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.execute(
                select(CareInterventionEvent).where(
                    CareInterventionEvent.participant_id == participant_id,
                    CareInterventionEvent.delivery_status.in_(("sent", "escalated")),
                ).order_by(
                    desc(CareInterventionEvent.sent_at),
                    desc(CareInterventionEvent.created_at),
                ).limit(1)
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def timeline(
        self, participant_id: uuid.UUID, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(CareInterventionEvent).where(
                    CareInterventionEvent.participant_id == participant_id
                ).order_by(desc(CareInterventionEvent.scheduled_at)).limit(
                    max(1, min(int(limit), 500))
                )
            ).scalars().all()
            return [self._view(row) for row in rows]

    def apply_action(
        self,
        participant_id: uuid.UUID,
        intervention_id: uuid.UUID,
        *,
        action: str,
        callback_event_id: str,
        optional_comment: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_action = str(action).strip().casefold()
        if normalized_action not in CARE_ACTIONS:
            raise ValueError("unsupported care action")
        event_key = str(callback_event_id or "").strip()[:160]
        if not event_key:
            raise ValueError("callback_event_id is required")
        changed_at = _aware(now or utc_now())
        comment = str(optional_comment or "").strip()[:500] or None
        try:
            with self.database.session() as session:
                existing = session.execute(
                    select(CareInterventionFeedback).where(
                        CareInterventionFeedback.callback_event_id == event_key
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return self._existing_action_result(
                        session,
                        existing,
                        participant_id=participant_id,
                        intervention_id=intervention_id,
                        action=normalized_action,
                    )
                candidate = session.get(CareInterventionEvent, intervention_id)
                if (
                    candidate is None
                    or candidate.participant_id != participant_id
                ):
                    raise ValueError("care intervention is not owned by participant")
                session.get(Participant, participant_id, with_for_update=True)
                intervention = session.execute(
                    select(CareInterventionEvent)
                    .where(CareInterventionEvent.id == intervention_id)
                    .with_for_update()
                ).scalar_one()
                if intervention.delivery_status not in {"sent", "escalated"}:
                    raise ValueError("care intervention has not been delivered")
                if (
                    normalized_action in OPERATIONAL_CARE_ACTIONS
                    and intervention.user_action is not None
                ):
                    return self._resolved_operational_result(
                        session,
                        intervention,
                        requested_action=normalized_action,
                    )

                feedback = CareInterventionFeedback(
                    intervention_id=intervention.id,
                    participant_id=participant_id,
                    helpfulness="helpful" if normalized_action == "helpful" else None,
                    relevance=(
                        "not_relevant"
                        if normalized_action == "not_relevant"
                        else "relevant" if normalized_action == "helpful" else None
                    ),
                    timing_feedback=(
                        "too_early" if normalized_action == "too_early"
                        else "too_late" if normalized_action == "too_late"
                        else None
                    ),
                    action_selected=normalized_action,
                    optional_comment=comment,
                    submitted_at=changed_at,
                    callback_event_id=event_key,
                )
                session.add(feedback)
                follow_up_id = None
                action_result = "feedback_recorded"
                if normalized_action == "snooze_30":
                    intervention.user_action = normalized_action
                    intervention.action_at = changed_at
                    intervention.updated_at = changed_at
                    intervention.status = "snoozed"
                    intervention.snoozed_until = changed_at + timedelta(minutes=30)
                    follow_up_id, action_result = self._create_snooze_in_session(
                        session, intervention, changed_at
                    )
                elif normalized_action == "mute_today":
                    intervention.user_action = normalized_action
                    intervention.action_at = changed_at
                    intervention.updated_at = changed_at
                    intervention.status = "muted"
                    self._mute_in_session(session, intervention, changed_at)
                    action_result = "recorded"
                elif normalized_action == "ack":
                    intervention.user_action = normalized_action
                    intervention.action_at = changed_at
                    intervention.updated_at = changed_at
                    intervention.status = "acknowledged"
                    action_result = "recorded"
                session.flush()
                return {
                    "created": True,
                    "action_result": action_result,
                    "follow_up_warning_id": follow_up_id,
                    "feedback": self._feedback_view(feedback),
                    "intervention": self._view(intervention),
                }
        except IntegrityError:
            with self.database.session() as session:
                existing = session.execute(
                    select(CareInterventionFeedback).where(
                        CareInterventionFeedback.callback_event_id == event_key
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return self._existing_action_result(
                        session,
                        existing,
                        participant_id=participant_id,
                        intervention_id=intervention_id,
                        action=normalized_action,
                    )
                intervention = session.get(CareInterventionEvent, intervention_id)
                if (
                    intervention is not None
                    and intervention.participant_id == participant_id
                    and normalized_action in OPERATIONAL_CARE_ACTIONS
                    and intervention.user_action is not None
                ):
                    return self._resolved_operational_result(
                        session,
                        intervention,
                        requested_action=normalized_action,
                    )
            raise

    def _create_snooze_in_session(
        self,
        session: Any,
        intervention: CareInterventionEvent,
        now: datetime,
    ) -> tuple[str | None, str]:
        existing_follow_up = session.execute(
            select(WarningSchedule).where(
                WarningSchedule.snoozed_from_intervention_id == intervention.id
            )
        ).scalar_one_or_none()
        if existing_follow_up is not None:
            return str(existing_follow_up.id), "scheduled"
        preference = session.get(
            ParticipantCarePreference, intervention.participant_id
        )
        preference_view = (
            self.preferences._view(preference)
            if preference is not None
            else self.preferences.defaults()
        )
        if not preference_view["allow_follow_up"]:
            return None, "follow_up_disabled"
        sent_count = len(
            session.execute(
                select(WarningSchedule.id).where(
                    WarningSchedule.participant_id == intervention.participant_id,
                    WarningSchedule.local_date
                    == _aware(intervention.scheduled_at).astimezone(
                        self.preferences.timezone
                    ).date(),
                    WarningSchedule.status.in_(("sent", "escalated")),
                )
            ).all()
        )
        if sent_count >= int(preference_view["effective_max_proactive_care_per_day"]):
            return None, "daily_cap"
        original = session.get(WarningSchedule, intervention.source_warning_id)
        if original is None:
            return None, "source_warning_unavailable"
        target = now + timedelta(minutes=30)
        local_date = target.astimezone(self.preferences.timezone).date()
        if local_date != original.local_date:
            return None, "cross_day_snooze_unavailable"
        warning_id = uuid.uuid4()
        identity = hashlib.sha256(
            f"{original.warning_identity}\0snooze\0{target.isoformat()}".encode("utf-8")
        ).hexdigest()
        payload = dict(original.payload_json)
        provenance = dict(payload.get("care_provenance") or {})
        provenance.update(
            {
                "source_warning_id": str(original.id),
                "source_forecast_id": str(original.forecast_id),
                "forecast_version": original.forecast_version,
            }
        )
        payload.update(
            {
                "care_provenance": provenance,
                "user_requested_followup": True,
                "snoozed_from_intervention_id": str(intervention.id),
            }
        )
        follow_up = WarningSchedule(
            id=warning_id,
            participant_id=original.participant_id,
            local_date=original.local_date,
            forecast_id=original.forecast_id,
            forecast_version=original.forecast_version,
            snoozed_from_intervention_id=intervention.id,
            warning_identity=identity,
            episode_identity=identity,
            target_time=target,
            risk_time=target + timedelta(minutes=15),
            valid_until=target + timedelta(minutes=10),
            warning_level=original.warning_level,
            status="pending",
            payload_json=payload,
            next_attempt_at=target,
            updated_at=now,
        )
        session.add(follow_up)
        session.flush()
        self.mirror_warning_in_session(session, follow_up)
        return str(warning_id), "scheduled"

    def _resolved_operational_result(
        self,
        session: Any,
        intervention: CareInterventionEvent,
        *,
        requested_action: str,
    ) -> dict[str, Any]:
        recorded_action = str(intervention.user_action)
        follow_up = session.execute(
            select(WarningSchedule).where(
                WarningSchedule.snoozed_from_intervention_id == intervention.id
            )
        ).scalar_one_or_none()
        same_action = recorded_action == requested_action
        return {
            "created": False,
            "action_result": (
                "scheduled"
                if same_action and requested_action == "snooze_30" and follow_up
                else "already_recorded" if same_action else "already_resolved"
            ),
            "requested_action": requested_action,
            "recorded_action": recorded_action,
            "follow_up_warning_id": str(follow_up.id) if follow_up else None,
            "feedback": None,
            "intervention": self._view(intervention),
        }

    def _mute_in_session(
        self,
        session: Any,
        intervention: CareInterventionEvent,
        now: datetime,
    ) -> None:
        local_date = now.astimezone(self.preferences.timezone).date()
        local_end = datetime.combine(
            local_date + timedelta(days=1), time.min, self.preferences.timezone
        ).astimezone(timezone.utc)
        preference = session.get(
            ParticipantCarePreference,
            intervention.participant_id,
            with_for_update=True,
        )
        if preference is None:
            preference = ParticipantCarePreference(
                participant_id=intervention.participant_id,
                version=0,
            )
            session.add(preference)
            session.flush()
        current = _aware(preference.muted_until) if preference.muted_until else None
        if current is None or current < local_end:
            preference.muted_until = local_end
            preference.version = int(preference.version or 0) + 1
            preference.updated_at = now
        self.preferences._cancel_disallowed_in_session(
            session,
            intervention.participant_id,
            preference,
            now=now,
        )

    def _existing_action_result(
        self,
        session: Any,
        feedback: CareInterventionFeedback,
        *,
        participant_id: uuid.UUID,
        intervention_id: uuid.UUID,
        action: str,
    ) -> dict[str, Any]:
        if (
            feedback.participant_id != participant_id
            or feedback.intervention_id != intervention_id
            or feedback.action_selected != action
        ):
            raise ValueError("callback_event_id was already used for another action")
        intervention = session.get(CareInterventionEvent, intervention_id)
        follow_up_warning_id = None
        action_result = "already_recorded"
        if action == "snooze_30":
            warning = session.execute(
                select(WarningSchedule).where(
                    WarningSchedule.snoozed_from_intervention_id == intervention_id
                )
            ).scalar_one_or_none()
            if warning is not None:
                follow_up_warning_id = str(warning.id)
                action_result = "scheduled"
        return {
            "created": False,
            "action_result": action_result,
            "follow_up_warning_id": follow_up_warning_id,
            "feedback": self._feedback_view(feedback),
            "intervention": self._view(intervention),
        }

    @staticmethod
    def _feedback_view(row: CareInterventionFeedback) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "intervention_id": str(row.intervention_id),
            "helpfulness": row.helpfulness,
            "relevance": row.relevance,
            "timing_feedback": row.timing_feedback,
            "action_selected": row.action_selected,
            "optional_comment": row.optional_comment,
            "submitted_at": _aware(row.submitted_at).isoformat(),
            "callback_event_id": row.callback_event_id,
        }

    @staticmethod
    def _view(row: CareInterventionEvent) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "source_warning_id": str(row.source_warning_id),
            "source_forecast_id": str(row.source_forecast_id),
            "forecast_version": row.forecast_version,
            "intervention_type": row.intervention_type,
            "template_id": row.template_id,
            "template_version": row.template_version,
            "reason_code": row.reason_code,
            "scheduled_at": _aware(row.scheduled_at).isoformat(),
            "sent_at": _aware(row.sent_at).isoformat() if row.sent_at else None,
            "status": row.status,
            "delivery_status": row.delivery_status,
            "user_action": row.user_action,
            "action_at": _aware(row.action_at).isoformat() if row.action_at else None,
            "snoozed_until": (
                _aware(row.snoozed_until).isoformat()
                if row.snoozed_until else None
            ),
            "message": row.message_text,
            "context": dict(row.context_json),
            "actions": list(row.actions_json),
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
        }
