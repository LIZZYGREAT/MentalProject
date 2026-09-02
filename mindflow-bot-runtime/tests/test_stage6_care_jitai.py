from datetime import date, datetime, timedelta, timezone
import uuid

import pytest

from app.models import (
    CareInterventionEvent,
    CareInterventionOutcome,
    ForecastSnapshot,
    WarningSchedule,
)
from app.repositories import ObservationRepository, ParticipantRepository
from app.repositories_care import (
    CareInterventionRepository,
    ParticipantCarePreferenceRepository,
)
from app.services.care_effectiveness import CareEffectivenessService
from app.services.care_jitai import INTERVENTION_OPTIONS
from app.services.care_message_service import CareMessageService
from app.services.care_what_if import CareWhatIfSimulationService
from helpers import memory_database


DAY = date(2030, 1, 15)
SENT = datetime(2030, 1, 15, 4, 0, tzinfo=timezone.utc)


def _contextual(**history):
    return CareMessageService("Asia/Shanghai").contextualize_alert(
        {
            "time": "12:00",
            "tier": 2,
            "S": 8.2,
            "V": 3.0,
            "F": 0.75,
            "continuous_hours": 2.0,
            "care_action": "protected_break",
            "current_events": ["连续课程"],
            "dominant_stressors": ["连续课程"],
        },
        source="forecast_warning",
        local_date=DAY,
        calendar_events=[
            {
                "id": "course",
                "summary": "连续课程",
                "event_type": "course",
                "start_time": "2030-01-15T11:00:00+08:00",
                "end_time": "2030-01-15T12:30:00+08:00",
            }
        ],
        calendar_degraded=False,
        recent_observation=None,
        profile=None,
        profile_version=None,
        care_preferences={
            "version": 1,
            "quiet_hours_start": None,
            "quiet_hours_end": None,
            "interruption_tolerance": 0.5,
        },
        care_history=history,
    )


def test_jitai_scores_are_bounded_auditable_and_use_logistic_receptivity():
    result = _contextual(
        previous_warning_interval_minutes=30,
        recent_dismissal=True,
    )
    plan = result["care_plan"]
    decision = plan["jitai_decision"]

    assert plan["option_type"] in INTERVENTION_OPTIONS
    assert 0 <= plan["vulnerability_score"] <= 1
    assert 0 <= plan["receptivity_score"] <= 1
    assert plan["decision_score"] == pytest.approx(
        plan["vulnerability_score"] * plan["receptivity_score"], abs=0.001
    )
    assert decision["receptivity_model_version"] == "receptivity-logistic-v1"
    assert decision["receptivity_features"]["recent_dismissal"] == 1.0
    assert decision["vulnerability_decomposition"]
    assert plan["decision_rule"] == "next_acceptable_window"
    assert plan["scheduled_at"].startswith("2030-01-15T12:30:00")


def _seed_intervention(database, participant_id):
    forecast_id = uuid.uuid4()
    warning_id = uuid.uuid4()
    with database.session() as session:
        session.add(
            ForecastSnapshot(
                id=forecast_id,
                participant_id=participant_id,
                local_date=DAY,
                calendar_revision="calendar",
                semantic_revision="semantic",
                observation_revision="observation",
                algorithm_version="model",
                forecast_version="forecast",
                semantic_status="rules_only",
                semantic_input_json=[],
                curve_json=[],
                peaks_json=[],
                warning_windows_json=[],
                output_json={},
                valid=True,
                generated_at=SENT - timedelta(hours=1),
            )
        )
        session.add(
            WarningSchedule(
                id=warning_id,
                participant_id=participant_id,
                local_date=DAY,
                forecast_id=forecast_id,
                forecast_version="forecast",
                warning_identity="warning",
                episode_identity="episode",
                target_time=SENT,
                risk_time=SENT + timedelta(minutes=20),
                valid_until=SENT + timedelta(minutes=10),
                warning_level="2",
                status="sent",
                payload_json={},
                authorized_at=SENT,
                sent_at=SENT,
                updated_at=SENT,
            )
        )
        session.add(
            CareInterventionEvent(
                id=warning_id,
                participant_id=participant_id,
                source_warning_id=warning_id,
                source_forecast_id=forecast_id,
                forecast_version="forecast",
                intervention_type="protected_break",
                template_id="protected-break-v1",
                template_version="1.0.0",
                reason_code="sustained_high_pressure",
                vulnerability_score=0.8,
                receptivity_score=0.7,
                decision_score=0.56,
                decision_json={"observational_only": True},
                scheduled_at=SENT,
                sent_at=SENT,
                status="sent",
                delivery_status="sent",
                message_text="test",
                context_json={
                    "care_context": {
                        "stress_0_10": 8.0,
                        "vitality_0_10": 3.0,
                        "risk_time": (SENT + timedelta(minutes=20)).isoformat(),
                        "current_events": ["course"],
                    }
                },
                actions_json=["helpful", "not_relevant", "snooze_30", "disable_type"],
                created_at=SENT,
                updated_at=SENT,
            )
        )
    return warning_id


def test_outcome_matching_respects_post_decision_windows_and_feedback_learning():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-OUTCOME")
    intervention_id = _seed_intervention(database, person.id)
    observations = ObservationRepository(database)
    observations.add(
        person.id,
        "checkin",
        {"stress_0_10": 6.5, "energy_0_10": 4},
        observed_at=SENT + timedelta(minutes=31),
        source_message_id="followup-30",
    )
    observations.add(
        person.id,
        "checkin",
        {"stress_0_10": 5.5, "energy_0_10": 5},
        observed_at=SENT + timedelta(minutes=61),
        source_message_id="followup-60",
    )
    effects = CareEffectivenessService(database, "Asia/Shanghai")
    result = effects.refresh_outcomes(person.id, as_of=SENT + timedelta(hours=2))

    assert result["created"] == 1
    with database.session() as session:
        outcome = session.get(CareInterventionOutcome, intervention_id)
        assert outcome.followup_30m["stress_change"] == -1.5
        assert outcome.followup_60m["stress_change"] == -2.5
        assert outcome.context_json["observational_only"] is True

    preferences = ParticipantCarePreferenceRepository(
        database, system_max_daily_sends=2, timezone_name="Asia/Shanghai"
    )
    repository = CareInterventionRepository(database, preferences)
    repository.apply_action(
        person.id,
        intervention_id,
        action="disable_type",
        callback_event_id="disable-protected-break",
        now=SENT + timedelta(hours=2),
    )
    learned = preferences.get(person.id)
    assert learned["disabled_intervention_types"] == ["protected_break"]
    assert learned["interruption_tolerance"] < 0.5
    with database.session() as session:
        outcome = session.get(CareInterventionOutcome, intervention_id)
        assert outcome.helpful_rating == 0.0
        assert outcome.user_action == "disable_type"


def test_descriptive_effects_never_claim_causality():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-EFFECT")
    intervention_id = _seed_intervention(database, person.id)
    with database.session() as session:
        session.add(
            CareInterventionOutcome(
                intervention_id=intervention_id,
                participant_id=person.id,
                baseline_state={"stress_0_10": 8.0},
                followup_30m={"stress_change": -1.0},
                followup_60m={"stress_change": -2.0},
                helpful_rating=1.0,
                context_json={"observational_only": True},
                created_at=SENT,
                updated_at=SENT,
            )
        )
    report = CareEffectivenessService(database, "Asia/Shanghai").descriptive_effects(
        DAY, DAY, participant_id=person.id
    )
    assert report["analysis_type"] == "observational_descriptive"
    assert report["causal_claim_allowed"] is False
    assert report["groups"][0]["causal_effect"] is None
    assert report["groups"][0]["stress_change_30m"] == -1.0


def test_what_if_uses_a_copy_and_returns_required_comparison_metrics():
    participant_id = uuid.uuid4()
    source_events = [{
        "id": "event-1",
        "summary": "任务",
        "start_time": "2030-01-15T18:00:00+08:00",
        "end_time": "2030-01-15T19:00:00+08:00",
    }]

    class Forecasts:
        def latest(self, *_args):
            return {
                "id": uuid.uuid4(),
                "forecast_version": "forecast",
                "generated_at": SENT,
                "semantic_input": [],
                "curve": [
                    {"stress_0_10": 8.0, "workload": 0.8},
                    {"stress_0_10": 7.5, "workload": 0.6},
                ],
                "output": {"initial_state": {"stress_0_10": 4, "vitality_0_10": 7}},
            }

    class Calendars:
        def get(self, *_args):
            return {"events": source_events, "degraded": False}

    class Profiles:
        def current(self, *_args):
            return None

    class Observations:
        def for_local_date(self, *_args, **_kwargs):
            return []

    class Prediction:
        def calculate(self, **kwargs):
            assert kwargs["calendar_events"][0]["start_time"].startswith("2030-01-16T14:00")
            return {"trajectory": [
                {"stress_0_10": 6.0, "workload": 0.3, "recovery_resource": 0.5},
                {"stress_0_10": 5.5, "workload": 0.2},
            ]}

    class Coordinator:
        forecasts = Forecasts()
        calendar_snapshots = Calendars()
        profiles = Profiles()
        learned_profiles = None
        observations = Observations()
        prediction = Prediction()
        from zoneinfo import ZoneInfo
        timezone = ZoneInfo("Asia/Shanghai")

    result = CareWhatIfSimulationService(Coordinator()).simulate(
        participant_id,
        DAY,
        event_id="event-1",
        new_start_time="2030-01-16T14:00:00+08:00",
        new_end_time="2030-01-16T15:00:00+08:00",
    )
    assert result["simulation_only"] is True
    assert result["calendar_mutated"] is False
    assert set(result["scenario"]) == {
        "peak_stress",
        "high_stress_duration_minutes",
        "mean_workload",
        "recovery_windows",
    }
    assert source_events[0]["start_time"] == "2030-01-15T18:00:00+08:00"
