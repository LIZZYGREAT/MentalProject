from datetime import date, datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    CareInterventionEvent,
    CareInterventionOutcome,
    ForecastSnapshot,
    InterventionRandomizationEvent,
    ParticipantCarePreference,
    WarningSchedule,
)
from app.contracts.warning import WarningDeliveryPolicyConfig
from app.repositories import (
    ObservationRepository,
    ParticipantRepository,
    WarningScheduleRepository,
)
from app.repositories_care import (
    CareInterventionRepository,
    ParticipantCarePreferenceRepository,
)
from app.services.care_effectiveness import (
    CareEffectivenessService,
    _recovery_episode_changes,
    _wilson_uncertainty,
)
from app.services.care_jitai import (
    INTERVENTION_OPTIONS,
    normalized_intervention_types,
)
from app.services.care_message_service import CareMessageService
from app.services.care_what_if import CareWhatIfSimulationService
from app.services.forecast_coordinator import ForecastCoordinator
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
    observations = ObservationRepository(database)
    observations.add(
        person.id,
        "checkin",
        {"stress_0_10": 8.0, "energy_0_10": 3},
        observed_at=SENT - timedelta(minutes=30),
        source_message_id="observed-baseline",
    )
    intervention_id = _seed_intervention(database, person.id)
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

    assert result == {"created": 0, "updated": 1}
    with database.session() as session:
        outcome = session.get(CareInterventionOutcome, intervention_id)
        assert outcome.baseline_state["observed_baseline"]["stress_0_10"] == 8.0
        assert outcome.baseline_state["predicted_baseline"]["stress_0_10"] == 8.0
        assert outcome.followup_30m["observed_stress_change"] == -1.5
        assert outcome.followup_60m["observed_stress_change"] == -2.5
        assert outcome.followup_30m["forecast_residual"] == -1.5
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
        assert outcome.helpful_rating is None
        assert outcome.user_action == "disable_type"
        assert outcome.context_json["type_opt_out"] is True


def test_descriptive_effects_never_claim_causality():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-EFFECT")
    intervention_id = _seed_intervention(database, person.id)
    with database.session() as session:
        session.add(
            CareInterventionOutcome(
                intervention_id=intervention_id,
                participant_id=person.id,
                baseline_state={
                    "predicted_baseline": {"stress_0_10": 8.0},
                    "observed_baseline": {"stress_0_10": 8.0},
                },
                followup_30m={
                    "observed_stress_change": -1.0,
                    "forecast_residual": -1.0,
                },
                followup_60m={
                    "observed_stress_change": -2.0,
                    "forecast_residual": -2.0,
                },
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
    assert report["groups"][0]["observed_stress_change_30m"] == -1.0
    assert report["groups"][0]["forecast_residual_30m"] == -1.0


def test_research_effect_get_aggregation_is_read_only():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-READ-ONLY")
    intervention_id = _seed_intervention(database, person.id)
    report = CareEffectivenessService(database, "Asia/Shanghai").descriptive_effects(
        DAY, DAY, participant_id=person.id
    )
    assert report["groups"] == []
    with database.session() as session:
        assert session.get(CareInterventionOutcome, intervention_id) is None


def test_what_if_removes_moved_event_from_source_day():
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
        calls = 0

        def calculate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert kwargs["calendar_events"] == []
            else:
                assert kwargs["calendar_events"][0]["start_time"].startswith("2030-01-16T14:00")
            return {"trajectory": [
                {"stress_0_10": 6.0, "vitality_0_10": 5.0, "workload": 0.3, "recovery_resource": 0.5},
                {"stress_0_10": 5.5, "vitality_0_10": 5.5, "workload": 0.2},
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
        "recovery_window_count",
        "recovery_duration_minutes",
    }
    assert source_events[0]["start_time"] == "2030-01-15T18:00:00+08:00"


def test_what_if_propagates_terminal_state_across_intermediate_days():
    participant_id = uuid.uuid4()
    source_event = {
        "id": "event-1", "start_time": "2030-01-15T18:00:00+08:00",
        "end_time": "2030-01-15T19:00:00+08:00",
    }

    class Forecasts:
        def latest(self, _participant, target):
            return {
                "id": uuid.uuid5(uuid.NAMESPACE_DNS, target.isoformat()),
                "forecast_version": f"forecast-{target}", "generated_at": SENT,
                "semantic_input": [], "curve": [],
                "output": {"initial_state": {"stress_0_10": 4, "vitality_0_10": 7}},
            }

    class Calendars:
        def get(self, _participant, target):
            return {"events": [source_event] if target == DAY else [], "degraded": False}

    class Profiles:
        def current(self, *_args):
            return None

    class Observations:
        def for_local_date(self, *_args, **_kwargs):
            return []

    class Prediction:
        initial_states = []

        def calculate(self, **kwargs):
            self.initial_states.append(dict(kwargs["initial_state"]))
            terminal_stress = 6.0 - len(self.initial_states)
            return {"trajectory": [{
                "stress_0_10": terminal_stress,
                "vitality_0_10": 5.0 + len(self.initial_states),
            }]}

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
        participant_id, DAY, event_id="event-1",
        new_start_time="2030-01-18T14:00:00+08:00",
        new_end_time="2030-01-18T15:00:00+08:00",
    )
    assert result["affected_dates"] == [
        "2030-01-15", "2030-01-16", "2030-01-17", "2030-01-18"
    ]
    assert Coordinator.prediction.initial_states[1] == {
        "stress_0_10": 5.0, "vitality_0_10": 6.0
    }
    assert Coordinator.prediction.initial_states[2] == {
        "stress_0_10": 4.0, "vitality_0_10": 7.0
    }


def test_what_if_counts_contiguous_recovery_windows():
    metrics = CareWhatIfSimulationService._metrics([
        {"stress_0_10": 5, "recovery_resource": 0.5},
        {"stress_0_10": 4, "recovery_resource": 0.5},
        {"stress_0_10": 4, "recovery_resource": 0.0},
        {"stress_0_10": 3, "recovery_resource": 0.5},
    ])
    assert metrics["recovery_window_count"] == 2.0
    assert metrics["recovery_duration_minutes"] == 15.0


def test_after_risk_jitai_preserves_true_risk_time():
    coordinator = object.__new__(ForecastCoordinator)
    from zoneinfo import ZoneInfo

    coordinator.timezone = ZoneInfo("Asia/Shanghai")
    coordinator.warning_lead_minutes = 20
    coordinator.warning_late_grace_minutes = 10
    coordinator.warning_episode_drift_minutes = 15
    windows = coordinator._warning_windows(
        [{
            "time": "12:00",
            "tier": 2,
            "care_plan": {"scheduled_at": "2030-01-15T12:30:00+08:00"},
        }],
        DAY,
    )
    assert windows[0]["risk_time"].astimezone(coordinator.timezone).strftime("%H:%M") == "12:00"
    assert windows[0]["target_time"].astimezone(coordinator.timezone).strftime("%H:%M") == "12:30"


def test_after_risk_jitai_uses_separate_authorization_deadline():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-DEADLINE")
    forecast_id = uuid.uuid4()
    risk = datetime(2030, 1, 15, 4, 0, tzinfo=timezone.utc)
    target = risk + timedelta(minutes=30)
    with database.session() as session:
        session.add(ForecastSnapshot(
            id=forecast_id, participant_id=person.id, local_date=DAY,
            calendar_revision="c", semantic_revision="s", observation_revision="o",
            algorithm_version="a", forecast_version="f", semantic_status="rules_only",
            semantic_input_json=[], curve_json=[], peaks_json=[], warning_windows_json=[],
            output_json={}, valid=True, generated_at=risk - timedelta(hours=1),
        ))
    repository = WarningScheduleRepository(
        database,
        WarningDeliveryPolicyConfig(max_daily_sends=2, min_interval_minutes=0),
    )
    repository.sync(
        person.id, DAY, forecast_id=forecast_id, forecast_version="f",
        warnings=[{
            "warning_identity": "after-risk", "episode_identity": "after-risk",
            "target_time": target, "risk_time": risk,
            "authorization_deadline": target + timedelta(minutes=10),
            "valid_until": target + timedelta(minutes=10),
            "warning_level": "2", "payload": {},
        }],
        now=risk - timedelta(minutes=1),
    )
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        warning_id = row.id
        assert row.risk_time.replace(tzinfo=timezone.utc) == risk
        assert row.authorization_deadline.replace(tzinfo=timezone.utc) > target
    assert repository.claim_if_current(warning_id, now=target) is not None


def test_proximal_change_requires_observed_baseline():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-NO-BASELINE")
    intervention_id = _seed_intervention(database, person.id)
    ObservationRepository(database).add(
        person.id, "checkin", {"stress_0_10": 6.0},
        observed_at=SENT + timedelta(minutes=30), source_message_id="no-baseline-followup",
    )
    CareEffectivenessService(database, "Asia/Shanghai").refresh_outcomes(
        person.id, as_of=SENT + timedelta(hours=1)
    )
    with database.session() as session:
        outcome = session.get(CareInterventionOutcome, intervention_id)
        assert outcome.baseline_state["observed_baseline"] is None
        assert outcome.followup_30m["observed_stress_change"] is None
        assert outcome.followup_30m["forecast_residual"] == -2.0


def test_forecast_residual_is_not_named_observed_change():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-RESIDUAL")
    intervention_id = _seed_intervention(database, person.id)
    ObservationRepository(database).add(
        person.id, "checkin", {"stress_0_10": 7.0},
        observed_at=SENT + timedelta(minutes=30), source_message_id="residual-followup",
    )
    CareEffectivenessService(database, "Asia/Shanghai").refresh_outcomes(
        person.id, as_of=SENT + timedelta(hours=1)
    )
    with database.session() as session:
        followup = session.get(CareInterventionOutcome, intervention_id).followup_30m
        assert "stress_change" not in followup
        assert followup["forecast_residual"] == -1.0


def test_helpful_rate_uncertainty_uses_binary_samples():
    interval = _wilson_uncertainty([1.0, 1.0, 1.0, 0.0, 0.0])
    assert interval["method"] == "wilson"
    assert interval["standard_error"] > 0
    assert interval["lower_95"] < 0.6 < interval["upper_95"]


def test_recovery_evidence_counts_episode_not_five_minute_points():
    curve = [
        {"stress_0_10": 8.0, "recovery_resource": 0.0},
        *[
            {"stress_0_10": 7.5 - index * 0.2, "recovery_resource": 0.5}
            for index in range(6)
        ],
        {"stress_0_10": 6.0, "recovery_resource": 0.0},
    ]
    assert len(_recovery_episode_changes(curve)) == 1


def test_interruption_tolerance_zero_does_not_reset_to_half():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-ZERO")
    intervention_id = _seed_intervention(database, person.id)
    with database.session() as session:
        session.add(ParticipantCarePreference(
            participant_id=person.id, interruption_tolerance=0.0, version=1
        ))
    preferences = ParticipantCarePreferenceRepository(
        database, system_max_daily_sends=2, timezone_name="Asia/Shanghai"
    )
    CareInterventionRepository(database, preferences).apply_action(
        person.id, intervention_id, action="not_relevant",
        callback_event_id="zero-tolerance", now=SENT + timedelta(minutes=1),
    )
    assert preferences.get(person.id)["interruption_tolerance"] == 0.0


def test_disable_type_increments_preference_version_once():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-ONE-VERSION")
    intervention_id = _seed_intervention(database, person.id)
    preferences = ParticipantCarePreferenceRepository(
        database, system_max_daily_sends=2, timezone_name="Asia/Shanghai"
    )
    before = preferences.get(person.id)["version"]
    CareInterventionRepository(database, preferences).apply_action(
        person.id, intervention_id, action="disable_type",
        callback_event_id="disable-once", now=SENT + timedelta(minutes=1),
    )
    assert preferences.get(person.id)["version"] == before + 1


def test_repeated_feedback_does_not_repeat_learning():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-IDEMPOTENT-LEARNING")
    intervention_id = _seed_intervention(database, person.id)
    preferences = ParticipantCarePreferenceRepository(
        database, system_max_daily_sends=2, timezone_name="Asia/Shanghai"
    )
    repository = CareInterventionRepository(database, preferences)
    repository.apply_action(person.id, intervention_id, action="helpful", callback_event_id="help-1")
    learned = preferences.get(person.id)
    repository.apply_action(person.id, intervention_id, action="helpful", callback_event_id="help-2")
    repeated = preferences.get(person.id)
    assert repeated["version"] == learned["version"]
    assert repeated["interruption_tolerance"] == learned["interruption_tolerance"]


def test_candidate_receptivity_uses_candidate_decision_time():
    first = _contextual(last_intervention_at="2030-01-15T09:00:00+08:00")
    service = CareMessageService("Asia/Shanghai")
    later = service.contextualize_alert(
        {"time": "18:00", "tier": 2, "S": 8.2, "V": 3.0, "F": 0.75},
        source="forecast_warning", local_date=DAY, calendar_events=[],
        calendar_degraded=False, recent_observation=None, profile=None,
        profile_version=None, care_preferences={"interruption_tolerance": 0.5},
        care_history={"last_intervention_at": "2030-01-15T09:00:00+08:00"},
    )
    first_interval = first["care_plan"]["jitai_decision"]["receptivity_features"]["previous_warning_interval"]
    later_interval = later["care_plan"]["jitai_decision"]["receptivity_features"]["previous_warning_interval"]
    assert first_interval != later_interval


def test_stage6_orm_constraints_match_migration():
    assert "ck_care_jitai_scores" in {c.name for c in CareInterventionEvent.__table__.constraints}
    assert "ck_care_outcome_helpful" in {c.name for c in CareInterventionOutcome.__table__.constraints}
    assert "ck_mrt_probability" in {c.name for c in InterventionRandomizationEvent.__table__.constraints}
    assert "ck_care_interruption_tolerance" in {c.name for c in ParticipantCarePreference.__table__.constraints}
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-CONSTRAINT")
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(ParticipantCarePreference(
                participant_id=person.id,
                interruption_tolerance=1.5,
            ))


def test_explicit_disabled_type_can_be_explicitly_reenabled():
    database = memory_database()
    person = ParticipantRepository(database).create("STAGE6-REENABLE")
    intervention_id = _seed_intervention(database, person.id)
    preferences = ParticipantCarePreferenceRepository(
        database, system_max_daily_sends=2, timezone_name="Asia/Shanghai"
    )
    CareInterventionRepository(database, preferences).apply_action(
        person.id, intervention_id, action="disable_type", callback_event_id="disable-reenable"
    )
    assert preferences.get(person.id)["disabled_intervention_types"] == ["protected_break"]
    updated = preferences.update(
        person.id, {"reenable_intervention_types": ["protected_break"]}
    )
    assert updated["disabled_intervention_types"] == []


def test_preference_vocabulary_normalizes_legacy_values():
    assert normalized_intervention_types([
        "hydration", "walk", "recovery", "trusted_person", "task_decomposition"
    ]) == ["hydration_movement", "priority_review", "social_support"]
    assert normalized_intervention_types(INTERVENTION_OPTIONS) == sorted(
        INTERVENTION_OPTIONS
    )
    with pytest.raises(ValueError, match="unsupported intervention type"):
        normalized_intervention_types(["unknown_support"])
