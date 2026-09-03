import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.admin_web.repositories import AdminRepository
from app.contracts.warning import WarningDeliveryPolicyConfig
from app.integrations.feishu.cards import care_intervention_card
from app.models import (
    CareInterventionEvent,
    CareInterventionFeedback,
    Participant,
    ParticipantCarePreference,
    StateObservation,
    WarningSchedule,
)
from app.repositories import (
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
)
from app.repositories_care import (
    CareInterventionRepository,
    ParticipantCarePreferenceRepository,
)
from app.repositories_daily_review import DailyReviewScheduleRepository
from app.services.card_action_service import CardActionService
from app.services.care_message_service import CareMessageService
from app.services.daily_review_scheduler import DailyReviewScheduler
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.forecast_scheduler import ForecastScheduler
from app.services.warning_policy import WarningPolicy
from app.tools.care import _public_care_preferences
from helpers import memory_database, warning_repository


DAY = date(2030, 1, 15)
NOW = datetime(2030, 1, 15, 2, 0, tzinfo=timezone.utc)


def _forecast(database, participant_id, version="care-phase-a"):
    return ForecastSnapshotRepository(database).save(
        participant_id,
        DAY,
        calendar_revision="calendar-v1",
        semantic_revision="semantic-v1",
        observation_revision="observation-v1",
        algorithm_version="algorithm-v1",
        forecast_version=version,
        semantic_status="rules_only",
        semantic_input=[],
        curve=[],
        peaks=[],
        warning_windows=[],
        output={},
    )


def _window(identity, target, *, message=None):
    return {
        "episode_identity": identity,
        "target_time": target,
        "valid_until": target + timedelta(minutes=10),
        "risk_time": target + timedelta(minutes=20),
        "warning_level": "2",
        "episode_drift_minutes": 15,
        "payload": {
            "message": message or f"care message {identity}",
            "care_context": {
                "schema_version": "care-context-v1",
                "risk_time": (target + timedelta(minutes=20)).isoformat(),
            },
            "care_plan": {
                "intervention_type": "transition_buffer",
                "reason_code": "forecast_warning",
                "actions": [
                    "ack",
                    "snooze_30",
                    "mute_today",
                    "helpful",
                    "not_relevant",
                ],
            },
            "care_provenance": {
                "template_id": "transition-buffer-v1",
                "template_version": "1.0.0",
            },
        },
    }


def _setup(*, code="CARE-A", second_pending=False):
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create(code)
    warnings = warning_repository(
        database, max_daily_sends=2, min_interval_minutes=240
    )
    forecast = _forecast(database, participant.id)
    windows = [_window("episode-a", NOW + timedelta(minutes=1))]
    if second_pending:
        windows.append(_window("episode-b", NOW + timedelta(hours=5)))
    warnings.sync(
        participant.id,
        DAY,
        forecast_id=uuid.UUID(forecast["id"]),
        forecast_version=forecast["forecast_version"],
        warnings=windows,
        now=NOW,
    )
    preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=2,
        timezone_name="Asia/Shanghai",
    )
    interventions = CareInterventionRepository(database, preferences)
    return (
        database,
        participants,
        participant,
        warnings,
        preferences,
        interventions,
    )


def _send_first(database, warnings):
    with database.session() as session:
        warning = session.query(WarningSchedule).order_by(
            WarningSchedule.target_time
        ).first()
        warning_id = warning.id
        forecast_version = warning.forecast_version
        target = warning.target_time.replace(tzinfo=timezone.utc)
    claimed = warnings.claim_if_current(warning_id, now=target)
    assert claimed is not None
    assert warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=forecast_version,
        now=target + timedelta(milliseconds=500),
    )
    assert warnings.finish_claim(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=forecast_version,
        sent=True,
        now=target + timedelta(seconds=1),
    )
    return warning_id, target + timedelta(seconds=1)


def _set_warning_intervention(database, intervention_type):
    with database.session() as session:
        warning = session.query(WarningSchedule).one()
        payload = dict(warning.payload_json)
        payload["care_plan"] = {
            **dict(payload.get("care_plan") or {}),
            "intervention_type": intervention_type,
        }
        warning.payload_json = payload
        return (
            warning.id,
            warning.target_time.replace(tzinfo=timezone.utc),
            warning.forecast_version,
        )


def test_warning_lifecycle_is_mirrored_to_normalized_care_event():
    database, _, participant, warnings, _, interventions = _setup()

    with database.session() as session:
        warning = session.query(WarningSchedule).one()
        care = session.query(CareInterventionEvent).one()
        assert care.id == warning.id
        assert care.source_warning_id == warning.id
        assert care.source_forecast_id == warning.forecast_id
        assert care.forecast_version == warning.forecast_version
        assert care.intervention_type == "micro_break"
        assert care.status == "pending"

    warning_id, _ = _send_first(database, warnings)
    timeline = interventions.timeline(participant.id)
    assert timeline[0]["id"] == str(warning_id)
    assert timeline[0]["status"] == "sent"
    assert timeline[0]["delivery_status"] == "sent"
    assert timeline[0]["context"]["care_provenance"]["template_id"] == (
        "transition-buffer-v1"
    )


def test_controlled_care_preference_has_distinct_provenance_and_actions():
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        {
            "time": "10:00",
            "tier": 2,
            "S": 7.8,
            "V": 2.5,
            "F": 0.7,
            "trigger_source": "sustained_intensity",
            "care_action": "brief_check_in",
            "current_events": [],
            "dominant_stressors": [],
        },
        source="forecast_warning",
        local_date=DAY,
        calendar_events=[],
        calendar_degraded=False,
        recent_observation=None,
        profile={"care_preferences": {"recovery_preference": "补充药物"}},
        profile_version=99,
        care_preferences={
            "version": 4,
            "allow_follow_up": False,
            "preferred_support_types": ["walk"],
        },
    )

    assert contextual["care_provenance"]["profile_version"] is None
    assert contextual["care_provenance"]["care_preference_version"] == 4
    assert "补水" in contextual["message"]
    assert "药物" not in contextual["message"]
    assert "snooze_30" not in contextual["care_plan"]["actions"]
    assert contextual["care_plan"]["actions"] == (
        "ack",
        "helpful",
        "not_relevant",
        "mute_today",
        "disable_type",
    )

    legacy_fallback = CareMessageService(
        "Asia/Shanghai"
    ).contextualize_alert(
        {
            "time": "10:00",
            "tier": 2,
            "S": 7.8,
            "V": 2.5,
            "F": 0.7,
            "trigger_source": "sustained_intensity",
            "care_action": "brief_check_in",
        },
        source="forecast_warning",
        local_date=DAY,
        calendar_events=[],
        calendar_degraded=False,
        recent_observation=None,
        profile={"care_preferences": {"recovery_preference": "我喜欢散步"}},
        profile_version=7,
        care_preferences={
            "version": 0,
            "preferred_support_types": [],
        },
    )
    assert legacy_fallback["care_provenance"]["profile_version"] == 7
    assert legacy_fallback["care_provenance"]["care_preference_version"] is None


def test_repository_persists_first_explicit_empty_support_preference_as_authoritative():
    database = memory_database()
    participant = ParticipantRepository(database).create("CARE-EXPLICIT-EMPTY")
    preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=2,
        timezone_name="Asia/Shanghai",
    )

    stored = preferences.update(
        participant.id,
        {"preferred_support_types": []},
        now=NOW,
    )
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        {
            "time": "10:00",
            "tier": 2,
            "S": 7.8,
            "V": 2.5,
            "F": 0.7,
            "trigger_source": "sustained_intensity",
            "care_action": "brief_check_in",
        },
        source="forecast_warning",
        local_date=DAY,
        calendar_events=[],
        calendar_degraded=False,
        recent_observation=None,
        profile={"care_preferences": {"recovery_preference": "我喜欢散步"}},
        profile_version=7,
        care_preferences=stored,
    )

    assert stored["version"] == 1
    assert stored["preferred_support_types"] == []
    assert contextual["care_provenance"]["care_preference_version"] == 1
    assert contextual["care_provenance"]["profile_version"] is None
    assert contextual["care_context"]["profile_summary"][
        "recovery_preference"
    ] is None


def test_future_reserved_preferences_are_not_exposed_by_user_tools():
    public = _public_care_preferences({
        "care_enabled": True,
        "morning_brief_enabled": False,
        "weekly_summary_enabled": False,
        "version": 1,
    })

    assert public == {"care_enabled": True, "version": 1}


def test_high_vulnerability_in_quiet_hours_moves_to_next_acceptable_window():
    database = memory_database()
    preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=2,
        timezone_name="Asia/Shanghai",
    )
    coordinator = object.__new__(ForecastCoordinator)
    coordinator.timezone = preferences.timezone
    coordinator.warning_lead_minutes = 20
    coordinator.warning_late_grace_minutes = 10
    coordinator.warning_episode_drift_minutes = 15
    coordinator.warning_policy = WarningPolicy(
        WarningDeliveryPolicyConfig(
            max_daily_sends=2,
            min_interval_minutes=240,
        )
    )
    coordinator.care_preferences = preferences
    coordinator.care_messages = CareMessageService("Asia/Shanghai")
    alerts = [
        {
            "time": risk_time,
            "tier": 2,
            "S": 7.5,
            "V": 4.0,
            "F": 0.5,
            "episode_identity": f"episode-{index}",
            "trigger_source": "sustained_intensity",
            "care_action": "brief_check_in",
        }
        for index, risk_time in enumerate(("10:00", "15:00", "20:00"))
    ]

    selected, windows, _ = coordinator._derive_warning_state(
        {"alerts": alerts},
        DAY,
        {
            "care_preferences": {
                **preferences.defaults(),
                "quiet_hours_start": "09:00",
                "quiet_hours_end": "11:00",
            }
        },
    )

    assert [item["time"] for item in selected] == ["10:00", "15:00"]
    assert selected[0]["care_plan"]["decision_rule"] == "next_acceptable_window"
    assert selected[0]["care_plan"]["scheduled_at"].startswith(
        f"{DAY.isoformat()}T11:00:00"
    )
    assert windows[0]["target_time"].astimezone(preferences.timezone).hour == 11
    assert windows[0]["risk_time"] < windows[0]["target_time"]
    assert windows[0]["authorization_deadline"] > windows[0]["target_time"]


def test_user_preferences_are_stricter_than_system_cap_and_cancel_disallowed():
    database, _, participant, warnings, preferences, _ = _setup(
        second_pending=True
    )
    _send_first(database, warnings)

    with pytest.raises(ValueError, match="safety cap"):
        preferences.update(
            participant.id,
            {"max_proactive_care_per_day": 3},
            now=NOW + timedelta(minutes=2),
        )

    updated = preferences.update(
        participant.id,
        {
            "max_proactive_care_per_day": 1,
            "quiet_hours_start": "09:00",
            "quiet_hours_end": "11:00",
            "preferred_support_types": ["walk", "hydration"],
        },
        now=NOW + timedelta(minutes=2),
    )
    assert updated["version"] == 1
    assert updated["effective_max_proactive_care_per_day"] == 1
    assert updated["preferred_support_types"] == ["hydration_movement"]

    with database.session() as session:
        rows = session.query(WarningSchedule).order_by(
            WarningSchedule.target_time
        ).all()
        assert [row.status for row in rows] == ["sent", "cancelled"]
        care_rows = session.query(CareInterventionEvent).order_by(
            CareInterventionEvent.scheduled_at
        ).all()
        assert [row.delivery_status for row in care_rows] == [
            "sent",
            "cancelled",
        ]

    # A later forecast cannot reopen capacity after one successful send. The
    # participant cap is rechecked transactionally at claim time.
    later_forecast = _forecast(
        database, participant.id, version="care-phase-a-later"
    )
    later_target = NOW + timedelta(hours=8)
    warnings.sync(
        participant.id,
        DAY,
        forecast_id=uuid.UUID(later_forecast["id"]),
        forecast_version=later_forecast["forecast_version"],
        warnings=[_window("episode-c", later_target)],
        now=NOW + timedelta(minutes=3),
    )
    with database.session() as session:
        later = session.query(WarningSchedule).filter(
            WarningSchedule.forecast_version == "care-phase-a-later"
        ).one()
        later_id = later.id
    assert warnings.claim_if_current(later_id, now=later_target) is None
    with database.session() as session:
        assert session.get(WarningSchedule, later_id).status == "suppressed"

    quiet_preferences = {
        **preferences.defaults(),
        "quiet_hours_start": "09:00",
        "quiet_hours_end": "11:00",
    }
    local_ten = datetime(2030, 1, 15, 2, 0, tzinfo=timezone.utc)
    local_noon = datetime(2030, 1, 15, 4, 0, tzinfo=timezone.utc)
    assert not preferences.allows_scheduled_at(quiet_preferences, local_ten)
    assert preferences.allows_scheduled_at(quiet_preferences, local_noon)


def test_snooze_is_idempotent_restart_safe_and_bypasses_only_minimum_interval():
    database, _, participant, warnings, _, interventions = _setup()
    intervention_id, sent_at = _send_first(database, warnings)

    first = interventions.apply_action(
        participant.id,
        intervention_id,
        action="snooze_30",
        callback_event_id="callback-snooze-1",
        now=sent_at + timedelta(minutes=1),
    )
    replay = interventions.apply_action(
        participant.id,
        intervention_id,
        action="snooze_30",
        callback_event_id="callback-snooze-1",
        now=sent_at + timedelta(minutes=2),
    )
    replay_with_new_callback = interventions.apply_action(
        participant.id,
        intervention_id,
        action="snooze_30",
        callback_event_id="callback-snooze-2",
        now=sent_at + timedelta(minutes=3),
    )
    conflicting_action = interventions.apply_action(
        participant.id,
        intervention_id,
        action="ack",
        callback_event_id="callback-ack-after-snooze",
        now=sent_at + timedelta(minutes=4),
    )

    assert first["created"] is True
    assert first["action_result"] == "scheduled"
    assert replay["created"] is False
    assert replay["follow_up_warning_id"] == first["follow_up_warning_id"]
    assert replay_with_new_callback["created"] is False
    assert replay_with_new_callback["follow_up_warning_id"] == first[
        "follow_up_warning_id"
    ]
    assert conflicting_action["action_result"] == "already_resolved"
    assert conflicting_action["recorded_action"] == "snooze_30"
    with database.session() as session:
        assert session.query(WarningSchedule).count() == 2
        assert session.query(CareInterventionFeedback).count() == 1
        follow_up = session.get(
            WarningSchedule, uuid.UUID(first["follow_up_warning_id"])
        )
        assert follow_up.snoozed_from_intervention_id == intervention_id
        assert follow_up.payload_json["care_provenance"]["source_warning_id"] == str(
            intervention_id
        )
        claim_at = follow_up.target_time.replace(tzinfo=timezone.utc)

    # The original send is less than the four-hour minimum interval ago. A
    # user-requested follow-up may bypass that interval, but the daily cap
    # remains authoritative.
    claimed = warnings.claim_if_current(follow_up.id, now=claim_at)
    assert claimed is not None


def test_rejected_snooze_does_not_mark_intervention_snoozed():
    database, _, participant, warnings, preferences, interventions = _setup()
    intervention_id, sent_at = _send_first(database, warnings)
    preferences.update(
        participant.id, {"allow_follow_up": False}, now=sent_at
    )

    result = interventions.apply_action(
        participant.id,
        intervention_id,
        action="snooze_30",
        callback_event_id="callback-snooze-disabled",
        now=sent_at + timedelta(minutes=1),
    )

    assert result["action_result"] == "follow_up_disabled"
    assert result["follow_up_warning_id"] is None
    assert result["intervention"]["status"] == "sent"
    assert result["intervention"]["user_action"] is None
    with database.session() as session:
        assert session.query(WarningSchedule).count() == 1


def test_disabling_follow_up_cancels_pending_user_requested_warning():
    database, _, participant, warnings, preferences, interventions = _setup()
    intervention_id, sent_at = _send_first(database, warnings)
    snoozed = interventions.apply_action(
        participant.id,
        intervention_id,
        action="snooze_30",
        callback_event_id="callback-snooze-before-disable",
        now=sent_at + timedelta(minutes=1),
    )

    preferences.update(
        participant.id,
        {"allow_follow_up": False},
        now=sent_at + timedelta(minutes=2),
    )

    with database.session() as session:
        child = session.get(
            WarningSchedule, uuid.UUID(snoozed["follow_up_warning_id"])
        )
        assert child.status == "cancelled"
        assert child.payload_json["cancellation_reason"] == (
            "participant_care_preference"
        )


def test_warning_preference_change_before_final_authorization_cancels_claim():
    database, _, participant, warnings, preferences, _ = _setup()
    with database.session() as session:
        warning = session.query(WarningSchedule).one()
        warning_id = warning.id
        target = warning.target_time.replace(tzinfo=timezone.utc)
        version = warning.forecast_version
    claimed = warnings.claim_if_current(warning_id, now=target)
    assert claimed is not None

    preferences.update(
        participant.id,
        {"warning_enabled": False},
        now=target + timedelta(seconds=1),
    )

    assert not warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=version,
        now=target + timedelta(seconds=2),
    )
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "cancelled"


def test_warning_participant_deactivation_before_final_authorization_cancels_claim():
    database, _, participant, warnings, _, _ = _setup()
    with database.session() as session:
        warning = session.query(WarningSchedule).one()
        warning_id = warning.id
        target = warning.target_time.replace(tzinfo=timezone.utc)
        version = warning.forecast_version
    claimed = warnings.claim_if_current(warning_id, now=target)
    assert claimed is not None
    with database.session() as session:
        session.get(Participant, participant.id).status = "inactive"

    assert not warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=version,
        now=target + timedelta(seconds=1),
    )
    with database.session() as session:
        warning = session.get(WarningSchedule, warning_id)
        assert warning.status == "cancelled"
        assert warning.payload_json["cancellation_reason"] == "participant_inactive"


def test_warning_authorization_commit_defines_in_flight_preference_boundary():
    database, _, participant, warnings, preferences, _ = _setup()
    with database.session() as session:
        warning = session.query(WarningSchedule).one()
        warning_id = warning.id
        target = warning.target_time.replace(tzinfo=timezone.utc)
        version = warning.forecast_version
    claimed = warnings.claim_if_current(warning_id, now=target)
    assert claimed is not None
    assert warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=version,
        now=target + timedelta(seconds=1),
    )

    preferences.update(
        participant.id,
        {"warning_enabled": False},
        now=target + timedelta(seconds=2),
    )
    with database.session() as session:
        authorized = session.get(WarningSchedule, warning_id)
        assert authorized.status == "claimed"
        assert authorized.authorized_at is not None
    assert warnings.finish_claim(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=version,
        sent=True,
        now=target + timedelta(seconds=3),
    )


def test_disabling_schedule_suggestions_cancels_pending_schedule_adjustment():
    database, _, participant, _, preferences, _ = _setup()
    preferences.update(
        participant.id, {"allow_schedule_suggestions": True}, now=NOW
    )
    warning_id, _target, _version = _set_warning_intervention(
        database, "schedule_adjustment"
    )

    preferences.update(
        participant.id,
        {"allow_schedule_suggestions": False},
        now=NOW + timedelta(seconds=1),
    )

    with database.session() as session:
        warning = session.get(WarningSchedule, warning_id)
        assert warning.status == "cancelled"
        assert warning.payload_json["cancellation_reason"] == (
            "participant_schedule_suggestions_disabled"
        )


def test_disabling_schedule_suggestions_cancels_uncommitted_claim():
    database, _, participant, warnings, preferences, _ = _setup()
    preferences.update(
        participant.id, {"allow_schedule_suggestions": True}, now=NOW
    )
    warning_id, target, _version = _set_warning_intervention(
        database, "schedule_adjustment"
    )
    claimed = warnings.claim_if_current(warning_id, now=target)
    assert claimed is not None
    assert claimed["authorized_at"] is None

    preferences.update(
        participant.id,
        {"allow_schedule_suggestions": False},
        now=target + timedelta(seconds=1),
    )

    with database.session() as session:
        warning = session.get(WarningSchedule, warning_id)
        assert warning.status == "cancelled"
        assert warning.claim_token is None
        assert warning.payload_json["cancellation_reason"] == (
            "participant_schedule_suggestions_disabled"
        )


def test_authorized_schedule_suggestion_can_finish_after_preference_change():
    database, _, participant, warnings, preferences, _ = _setup()
    preferences.update(
        participant.id, {"allow_schedule_suggestions": True}, now=NOW
    )
    warning_id, target, version = _set_warning_intervention(
        database, "schedule_adjustment"
    )
    claimed = warnings.claim_if_current(warning_id, now=target)
    assert claimed is not None
    assert warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=version,
        now=target + timedelta(seconds=1),
    )

    preferences.update(
        participant.id,
        {"allow_schedule_suggestions": False},
        now=target + timedelta(seconds=2),
    )

    with database.session() as session:
        warning = session.get(WarningSchedule, warning_id)
        assert warning.status == "claimed"
        assert warning.authorized_at is not None
    assert warnings.finish_claim(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=version,
        sent=True,
        now=target + timedelta(seconds=3),
    )


def test_disabling_schedule_suggestions_does_not_cancel_other_care_types():
    database, _, participant, _, preferences, _ = _setup()
    preferences.update(
        participant.id, {"allow_schedule_suggestions": True}, now=NOW
    )
    warning_id, _target, _version = _set_warning_intervention(
        database, "transition_buffer"
    )

    preferences.update(
        participant.id,
        {"allow_schedule_suggestions": False},
        now=NOW + timedelta(seconds=1),
    )

    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "pending"


def test_final_claim_rejects_stale_schedule_adjustment_when_disabled():
    database, _, participant, warnings, preferences, _ = _setup()
    preferences.update(
        participant.id, {"allow_schedule_suggestions": True}, now=NOW
    )
    warning_id, target, _version = _set_warning_intervention(
        database, "schedule_adjustment"
    )
    # Simulate a persisted warning racing with a preference update after plan
    # generation. This intentionally bypasses the eager cancellation hook so
    # the independent final-authorization defense is exercised.
    with database.session() as session:
        preference = session.get(ParticipantCarePreference, participant.id)
        preference.allow_schedule_suggestions = False

    assert warnings.claim_if_current(warning_id, now=target) is None
    with database.session() as session:
        warning = session.get(WarningSchedule, warning_id)
        assert warning.status == "cancelled"
        assert warning.payload_json["cancellation_reason"] == (
            "participant_schedule_suggestions_disabled"
        )


def test_final_authorization_rejects_claimed_schedule_adjustment_when_disabled():
    database, _, participant, warnings, preferences, _ = _setup()
    preferences.update(
        participant.id, {"allow_schedule_suggestions": True}, now=NOW
    )
    warning_id, target, version = _set_warning_intervention(
        database, "schedule_adjustment"
    )
    claimed = warnings.claim_if_current(warning_id, now=target)
    assert claimed is not None
    with database.session() as session:
        preference = session.get(ParticipantCarePreference, participant.id)
        preference.allow_schedule_suggestions = False

    assert not warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=version,
        now=target + timedelta(seconds=1),
    )
    with database.session() as session:
        warning = session.get(WarningSchedule, warning_id)
        assert warning.status == "cancelled"
        assert warning.payload_json["cancellation_reason"] == (
            "participant_schedule_suggestions_disabled"
        )


def test_daily_review_preferences_cancel_queued_and_preserve_authorized_in_flight():
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create("CARE-DAILY-AUTH")
    preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=2,
        timezone_name="Asia/Shanghai",
    )
    schedules = DailyReviewScheduleRepository(
        database, timezone_name="Asia/Shanghai"
    )
    scheduled = NOW - timedelta(minutes=1)
    first = schedules.ensure(
        participant.id,
        DAY,
        scheduled,
        valid_until=NOW + timedelta(hours=1),
    )
    preferences.update(
        participant.id,
        {"daily_review_enabled": False},
        now=NOW,
    )
    assert schedules.get(first["id"])["status"] == "cancelled"

    preferences.update(
        participant.id,
        {"daily_review_enabled": True},
        now=NOW + timedelta(seconds=1),
    )
    second = schedules.ensure(
        participant.id,
        DAY + timedelta(days=1),
        scheduled,
        valid_until=NOW + timedelta(hours=1),
    )
    claimed = schedules.claim_due(NOW, lease_seconds=120)
    claimed_second = next(item for item in claimed if item["id"] == second["id"])
    assert schedules.authorize_claim_current(
        second["id"], claimed_second["claim_token"], now=NOW
    )

    preferences.update(
        participant.id,
        {"daily_review_enabled": False},
        now=NOW + timedelta(seconds=2),
    )
    assert schedules.get(second["id"])["status"] == "claimed"
    assert schedules.mark_sent(
        second["id"],
        claimed_second["claim_token"],
        now=NOW + timedelta(seconds=3),
        provider_message_id="om-authorized",
    )


def test_daily_review_final_authorization_rejects_inactive_participant():
    database = memory_database()
    participant = ParticipantRepository(database).create("CARE-DAILY-INACTIVE-AUTH")
    schedules = DailyReviewScheduleRepository(
        database, timezone_name="Asia/Shanghai"
    )
    scheduled = NOW - timedelta(minutes=1)
    review = schedules.ensure(
        participant.id,
        DAY,
        scheduled,
        valid_until=NOW + timedelta(hours=1),
    )
    claimed = schedules.claim_due(NOW, lease_seconds=120)
    claim = next(item for item in claimed if item["id"] == review["id"])
    with database.session() as session:
        session.get(Participant, participant.id).status = "inactive"

    assert not schedules.authorize_claim_current(
        review["id"], claim["claim_token"], now=NOW + timedelta(seconds=1)
    )
    stored = schedules.get(review["id"])
    assert stored["status"] == "cancelled"
    assert stored["last_error_code"] == "participant_inactive"


def test_mute_today_persists_and_cancels_only_the_same_participant():
    (
        database,
        participants,
        participant,
        warnings,
        preferences,
        interventions,
    ) = _setup(second_pending=True)
    intervention_id, sent_at = _send_first(database, warnings)
    other = participants.create("CARE-B")
    other_forecast = _forecast(database, other.id, version="care-phase-b")
    warnings.sync(
        other.id,
        DAY,
        forecast_id=uuid.UUID(other_forecast["id"]),
        forecast_version=other_forecast["forecast_version"],
        warnings=[_window("episode-other", NOW + timedelta(hours=6))],
        now=NOW,
    )

    interventions.apply_action(
        participant.id,
        intervention_id,
        action="mute_today",
        callback_event_id="callback-mute-1",
        now=sent_at + timedelta(minutes=1),
    )

    assert preferences.get(participant.id)["muted_until"] is not None
    assert preferences.get(other.id)["muted_until"] is None
    with database.session() as session:
        own_pending = session.query(WarningSchedule).filter(
            WarningSchedule.participant_id == participant.id,
            WarningSchedule.status == "pending",
        ).count()
        other_pending = session.query(WarningSchedule).filter(
            WarningSchedule.participant_id == other.id,
            WarningSchedule.status == "pending",
        ).count()
        assert own_pending == 0
        assert other_pending == 1


def test_card_actions_are_allowlisted_participant_bound_and_feedback_is_separate():
    database, participants, participant, warnings, _, interventions = _setup()
    intervention_id, _ = _send_first(database, warnings)
    card = care_intervention_card(
        intervention_id=str(intervention_id),
        message="一条经过审核的关怀消息",
        actions=["ack", "helpful", "unknown_action"],
    )
    buttons = [
        column["elements"][0]
        for column in card["body"]["elements"][1]["columns"]
    ]
    assert card["config"]["enable_forward"] is False
    assert {
        button["behaviors"][0]["value"]["mindflow_action"]
        for button in buttons
    } == {
        "care_ack",
        "care_helpful",
    }
    assert all(
        button["behaviors"][0]["value"]["intervention_id"]
        == str(intervention_id)
        for button in buttons
    )

    service = CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(
            on_observation_committed=lambda **_values: None
        ),
        care_interventions=interventions,
    )
    action_value = {
        "mindflow_action": "care_helpful",
        "version": "1",
        "intervention_id": str(intervention_id),
    }
    first = service.handle(
        participant.id,
        message_id="om-care",
        callback_event_id="callback-helpful-1",
        action_value=action_value,
        form_value={},
    )
    replay = service.handle(
        participant.id,
        message_id="om-care",
        callback_event_id="callback-helpful-1",
        action_value=action_value,
        form_value={},
    )
    assert first["created"] is True
    assert replay["created"] is False

    other = participants.create("CARE-OTHER")
    with pytest.raises(ValueError, match="not owned"):
        service.handle(
            other.id,
            message_id="om-care",
            callback_event_id="callback-cross-participant",
            action_value=action_value,
            form_value={},
        )
    with database.session() as session:
        assert session.query(CareInterventionFeedback).count() == 1
        assert session.query(StateObservation).count() == 0
        intervention = session.get(CareInterventionEvent, intervention_id)
        assert intervention.status == "sent"
        assert intervention.user_action is None


def test_admin_timeline_exposes_preferences_provenance_and_append_only_feedback():
    database, _, participant, warnings, preferences, interventions = _setup()
    intervention_id, sent_at = _send_first(database, warnings)
    preferences.update(
        participant.id,
        {"allow_follow_up": False, "preferred_support_types": ["walk"]},
        now=sent_at,
    )
    interventions.apply_action(
        participant.id,
        intervention_id,
        action="not_relevant",
        callback_event_id="callback-not-relevant-1",
        optional_comment="这次时间不合适",
        now=sent_at + timedelta(minutes=1),
    )

    timeline = AdminRepository(database).care_timeline(participant.id)
    assert timeline["preferences"]["allow_follow_up"] is False
    assert timeline["preferences"]["preferred_support_types"] == ["hydration_movement"]
    assert len(timeline["items"]) == 1
    item = timeline["items"][0]
    assert item["source_warning_id"] == str(intervention_id)
    assert item["forecast_version"] == "care-phase-a"
    assert item["template_id"] == "transition-buffer-v1"
    assert item["feedback"][0]["relevance"] == "not_relevant"
    assert item["feedback"][0]["optional_comment"] == "这次时间不合适"


def test_scheduler_uses_care_card_when_card_action_transport_is_available():
    database, participants, _, warnings, _, _ = _setup()

    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc-care"}

    class Sender:
        def __init__(self):
            self.calls = []

        def send_card(self, chat_id, card, *, message_uuid=None):
            self.calls.append(("card", chat_id, card, message_uuid))
            return "om-care-card"

        def send_text(self, chat_id, text, *, message_uuid=None):
            self.calls.append(("text", chat_id, text, message_uuid))
            return "om-care-text"

    sender = Sender()
    scheduler = ForecastScheduler(
        coordinator=SimpleNamespace(),
        participants=participants,
        warnings=warnings,
        bindings=Bindings(),
        sender=sender,
        timezone_name="Asia/Shanghai",
        daily_prepare_local_time="07:30",
        calendar_sync_interval_seconds=999,
        warning_poll_interval_seconds=999,
        calendar_oauth_app_id="calendar-app",
        care_card_enabled=True,
    )
    due = warnings.pending(NOW + timedelta(minutes=1))
    asyncio.run(scheduler._deliver_warning(due[0]))

    assert sender.calls[0][0] == "card"
    card = sender.calls[0][2]
    warning_id = sender.calls[0][3]
    assert card["schema"] == "2.0"
    assert card["config"]["enable_forward"] is False
    assert warning_id == card["body"]["elements"][1]["columns"][0][
        "elements"
    ][0]["behaviors"][0]["value"]["intervention_id"]


def _production_care_card(*, allow_follow_up: bool):
    database, participants, _, warnings, _, _ = _setup(
        code=f"CARE-PRODUCTION-{allow_follow_up}"
    )
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        {
            "time": "10:00",
            "tier": 2,
            "S": 7.8,
            "V": 2.5,
            "F": 0.7,
            "trigger_source": "sustained_intensity",
            "care_action": "brief_check_in",
            "current_events": [],
            "dominant_stressors": [],
        },
        source="forecast_warning",
        local_date=DAY,
        calendar_events=[],
        calendar_degraded=False,
        recent_observation=None,
        profile=None,
        profile_version=None,
        care_preferences={
            "version": 1,
            "allow_follow_up": allow_follow_up,
        },
    )
    with database.session() as session:
        warning = session.query(WarningSchedule).one()
        warning.payload_json = contextual

    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc-production-care"}

    class Sender:
        def __init__(self):
            self.card = None

        def send_card(self, _chat_id, card, *, message_uuid=None):
            self.card = card
            return "om-production-care"

        def send_text(self, *_args, **_kwargs):
            raise AssertionError("production Care must use an interactive card")

    sender = Sender()
    scheduler = ForecastScheduler(
        coordinator=SimpleNamespace(),
        participants=participants,
        warnings=warnings,
        bindings=Bindings(),
        sender=sender,
        timezone_name="Asia/Shanghai",
        daily_prepare_local_time="07:30",
        calendar_sync_interval_seconds=999,
        warning_poll_interval_seconds=999,
        calendar_oauth_app_id="calendar-app",
        care_card_enabled=True,
    )
    due = warnings.pending(NOW + timedelta(minutes=1))
    asyncio.run(scheduler._deliver_warning(due[0]))
    assert sender.card is not None
    return list(contextual["care_plan"]["actions"]), sender.card


def _card_actions_and_column_sizes(card):
    actions = []
    column_sizes = []
    for element in card["body"]["elements"]:
        if element.get("tag") != "column_set":
            continue
        columns = element.get("columns") or []
        column_sizes.append(len(columns))
        for column in columns:
            button = column["elements"][0]
            actions.append(button["behaviors"][0]["value"]["mindflow_action"])
    return actions, column_sizes


def test_production_care_delivery_preserves_all_six_policy_actions():
    policy_actions, card = _production_care_card(allow_follow_up=True)
    assert policy_actions == [
        "ack",
        "snooze_30",
        "helpful",
        "not_relevant",
        "mute_today",
        "disable_type",
    ]
    actions, column_sizes = _card_actions_and_column_sizes(card)
    assert actions == [f"care_{action}" for action in policy_actions]
    assert actions[-1] == "care_disable_type"
    assert column_sizes == [2, 2, 2]


def test_production_care_delivery_without_follow_up_removes_only_snooze():
    policy_actions, card = _production_care_card(allow_follow_up=False)
    assert policy_actions == [
        "ack",
        "helpful",
        "not_relevant",
        "mute_today",
        "disable_type",
    ]
    actions, column_sizes = _card_actions_and_column_sizes(card)
    assert actions == [f"care_{action}" for action in policy_actions]
    assert "care_snooze_30" not in actions
    assert "care_disable_type" in actions
    assert column_sizes == [2, 2, 1]


def test_care_card_delivery_recovers_after_restart_with_the_same_message_uuid(
    monkeypatch,
):
    database, participants, _, warnings, _, _ = _setup()

    class FixedDateTime(datetime):
        current = NOW + timedelta(minutes=1)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(
                tzinfo=None
            )

    class CrashAfterProviderSuccess(BaseException):
        pass

    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc-care"}

    class Sender:
        def __init__(self):
            self.crash = True
            self.attempts = []
            self.provider_messages = {}

        def send_card(self, _chat_id, _card, *, message_uuid=None):
            self.attempts.append(message_uuid)
            message_id = self.provider_messages.setdefault(
                message_uuid, f"om-{len(self.provider_messages) + 1}"
            )
            if self.crash:
                self.crash = False
                raise CrashAfterProviderSuccess()
            return message_id

    monkeypatch.setattr(
        "app.services.forecast_scheduler.datetime", FixedDateTime
    )
    sender = Sender()

    def scheduler():
        return ForecastScheduler(
            coordinator=SimpleNamespace(),
            participants=participants,
            warnings=warnings,
            bindings=Bindings(),
            sender=sender,
            timezone_name="Asia/Shanghai",
            daily_prepare_local_time="07:30",
            calendar_sync_interval_seconds=999,
            warning_poll_interval_seconds=999,
            calendar_oauth_app_id="calendar-app",
            warning_claim_lease_seconds=120,
            care_card_enabled=True,
        )

    due = warnings.pending(FixedDateTime.current)
    warning_id = due[0]["id"]
    with pytest.raises(CrashAfterProviderSuccess):
        asyncio.run(scheduler()._deliver_warning(due[0]))

    with database.session() as session:
        assert session.get(
            WarningSchedule, uuid.UUID(warning_id)
        ).status == "claimed"
        assert session.get(
            CareInterventionEvent, uuid.UUID(warning_id)
        ).delivery_status == "claimed"
        assert session.get(
            CareInterventionEvent, uuid.UUID(warning_id)
        ).status == "claimed"

    FixedDateTime.current += timedelta(seconds=121)
    recovered = warnings.pending(FixedDateTime.current)
    asyncio.run(scheduler()._deliver_warning(recovered[0]))

    assert sender.attempts == [warning_id, warning_id]
    assert sender.provider_messages == {warning_id: "om-1"}
    with database.session() as session:
        assert session.get(
            CareInterventionEvent, uuid.UUID(warning_id)
        ).status == "sent"


def test_disabling_daily_review_prevents_new_schedules_and_survives_restart():
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create("CARE-DAILY-REVIEW")
    preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=2,
        timezone_name="Asia/Shanghai",
    )
    preferences.update(
        participant.id,
        {"care_enabled": False},
        now=NOW,
    )

    class Schedules:
        def __init__(self):
            self.ensured = []

        def ensure(self, *args, **kwargs):
            self.ensured.append((args, kwargs))

        def reactivate_available(self, *_args):
            return None

        def claim_due(self, *_args):
            return []

    class Bindings:
        def get_for_participant(self, _participant_id):
            return None

    schedules = Schedules()
    scheduler = DailyReviewScheduler(
        schedules=schedules,
        participants=participants,
        bindings=Bindings(),
        forecasts=ForecastSnapshotRepository(database),
        sender=SimpleNamespace(),
        timezone_name="Asia/Shanghai",
        care_preferences=preferences,
    )
    local_2201 = datetime(2030, 1, 15, 14, 1, tzinfo=timezone.utc)
    counts = asyncio.run(scheduler.run_once(local_2201))

    restarted_preferences = ParticipantCarePreferenceRepository(
        database,
        system_max_daily_sends=2,
        timezone_name="Asia/Shanghai",
    )
    assert restarted_preferences.get(participant.id)[
        "care_enabled"
    ] is False
    assert schedules.ensured == []
    assert counts["ensured"] == 0
