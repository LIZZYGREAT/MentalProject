import asyncio
from datetime import date, datetime
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.agent.context import AgentContext
from app.models import WarningSchedule
from app.repositories import (
    CalendarSnapshotRepository,
    EventSemanticCacheRepository,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
    ProfileRepository,
)
from app.services.care_message_service import CareMessageService
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.forecast_coordinator import ForecastCoordinator
from app.tools.care import CareTools
from helpers import memory_database, warning_repository
from mindflow_core.assessment import AssessmentModel, sanitize_forecast_alert


TARGET = date(2030, 1, 15)
OLD_TIER_ONE = "可以用几分钟检查任务优先级、补水或活动一下；若不需要，也可忽略本次提示。"


def _event(
    event_id: str,
    summary: str,
    start: str,
    end: str,
    **values,
):
    return {
        "id": event_id,
        "summary": summary,
        "start_time": f"{TARGET.isoformat()}T{start}:00+08:00",
        "end_time": f"{TARGET.isoformat()}T{end}:00+08:00",
        **values,
    }


def _alert(**values):
    return {
        "time": "16:30",
        "tier": 1,
        "S": 75.0,
        "V": 55.0,
        "F": 0.45,
        "trigger_source": "sustained_intensity",
        "care_action": "brief_check_in",
        "current_events": ["高等数学"],
        "dominant_stressors": ["高等数学", "项目讨论"],
        "fallback_message": OLD_TIER_ONE,
        **values,
    }


def _calendar():
    return [
        _event("math", "高等数学", "15:30", "17:00", event_type="course"),
        _event("project", "项目讨论", "17:10", "18:30", event_type="task"),
    ]


def _observation(*, energy: float, stress: float = 5.0):
    return {
        "id": f"observation-{energy}",
        "observed_at": f"{TARGET.isoformat()}T12:00:00+08:00",
        "payload": {
            "stress_0_10": stress,
            "energy_0_10": energy,
            "activity": "准备上课",
        },
    }


def test_alert_sanitizer_preserves_bounded_context_without_json_passthrough():
    raw = {
        **_alert(),
        "current_events": ["项目讨论"] * 20,
        "dominant_stressors": ["高等数学", "项目讨论"],
        "policy": {"candidate_only": True, "unknown": {"nested": "value"}},
        "arbitrary": {"secret": "must not cross boundary"},
    }

    sanitized = sanitize_forecast_alert(raw)

    assert sanitized["current_events"] == ["项目讨论"] * 8
    assert sanitized["dominant_stressors"] == ["高等数学", "项目讨论"]
    assert sanitized["policy"] == {"candidate_only": True}
    assert sanitized["fallback_message"] == OLD_TIER_ONE
    assert "message" not in sanitized
    assert "arbitrary" not in sanitized


def test_assessment_model_boundary_preserves_alert_event_lists(monkeypatch):
    class Solver:
        def simulate_day(self, *_args, **_kwargs):
            return (
                [
                    {
                        "time": "16:30",
                        "S": 75.0,
                        "V": 55.0,
                        "state": "DAY_ACTIVE",
                        "current_events": ["项目讨论"],
                    }
                ],
                75.0,
                55.0,
                None,
                None,
                [
                    _alert(
                        current_events=["项目讨论"],
                        dominant_stressors=["高等数学", "项目讨论"],
                    )
                ],
                [0.8],
            )

    class User:
        def __init__(self, **_kwargs):
            self.solver = Solver()

        def get_param(self, _name, default=None):
            return default

        def get_current_S_star(self):
            return 50.0

        def get_current_threshold(self):
            return 70.0

    monkeypatch.setattr("mindflow_core.assessment.User", User)
    result = AssessmentModel("Asia/Shanghai").predict(
        profile={},
        observations=[],
        calendar_events=[],
        local_date=TARGET.isoformat(),
    )

    assert result.alerts[0]["current_events"] == ["项目讨论"]
    assert result.alerts[0]["dominant_stressors"] == [
        "高等数学",
        "项目讨论",
    ]


def test_same_tier_different_calendars_produce_different_interventions():
    care = CareMessageService("Asia/Shanghai")
    dense = care.contextualize_alert(
        _alert(),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=_calendar(),
        calendar_degraded=False,
        recent_observation=None,
        profile=None,
        profile_version=None,
    )
    deadline = care.contextualize_alert(
        _alert(
            time="15:30",
            current_events=["项目截止"],
            dominant_stressors=["项目截止"],
        ),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=[
            _event(
                "deadline",
                "项目报告截止",
                "15:00",
                "17:00",
                event_type="task",
                task_type="ddl",
            )
        ],
        calendar_degraded=False,
        recent_observation=None,
        profile=None,
        profile_version=None,
    )

    assert dense["care_plan"]["intervention_type"] == "transition_buffer"
    assert deadline["care_plan"]["intervention_type"] == "workload_decomposition"
    assert dense["message"] != deadline["message"]
    assert "高等数学" in dense["message"]
    assert "项目报告截止" in deadline["message"]
    assert dense["message"] != OLD_TIER_ONE
    assert 80 <= len(dense["message"]) <= 220
    assert 80 <= len(deadline["message"]) <= 220


def test_same_calendar_low_energy_uses_recovery_instead_of_transition_buffer():
    care = CareMessageService("Asia/Shanghai")
    low = care.contextualize_alert(
        _alert(),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=_calendar(),
        calendar_degraded=False,
        recent_observation=_observation(energy=2.0),
        profile={"care_preferences": {"recovery_preference": "短暂散步"}},
        profile_version=4,
    )
    normal = care.contextualize_alert(
        _alert(),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=_calendar(),
        calendar_degraded=False,
        recent_observation=_observation(energy=7.0),
        profile={"care_preferences": {"recovery_preference": "短暂散步"}},
        profile_version=4,
    )

    assert low["care_plan"]["intervention_type"] == "recovery"
    assert normal["care_plan"]["intervention_type"] == "transition_buffer"
    assert "精力偏低" in low["message"]
    assert "短暂散步" in low["message"]
    assert low["care_provenance"]["observation_id"] == "observation-2.0"
    assert low["care_provenance"]["profile_version"] == 4
    assert 80 <= len(low["message"]) <= 220
    assert 80 <= len(normal["message"]) <= 220


def test_recent_observation_window_is_inclusive_at_six_hours_and_rejects_older():
    care = CareMessageService("Asia/Shanghai")

    def contextual(observed_at: str):
        observation = _observation(energy=2.0)
        observation["observed_at"] = observed_at
        return care.contextualize_alert(
            _alert(time="16:30"),
            source="forecast_warning",
            local_date=TARGET,
            calendar_events=_calendar(),
            calendar_degraded=False,
            recent_observation=observation,
            profile=None,
            profile_version=None,
        )

    boundary = contextual(f"{TARGET.isoformat()}T10:30:00+08:00")
    stale = contextual(f"{TARGET.isoformat()}T10:29:59+08:00")

    assert boundary["care_context"]["recent_observation"] is not None
    assert boundary["care_plan"]["intervention_type"] == "recovery"
    assert stale["care_context"]["recent_observation"] is None
    assert stale["care_plan"]["intervention_type"] == "transition_buffer"
    assert stale["care_provenance"]["recent_observation_max_age_minutes"] == 360


def test_versioned_empty_controlled_preferences_do_not_fall_back_to_profile():
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        _alert(),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=_calendar(),
        calendar_degraded=False,
        recent_observation=None,
        profile={"care_preferences": {"recovery_preference": "我喜欢散步"}},
        profile_version=11,
        care_preferences={
            "version": 1,
            "preferred_support_types": [],
            "allow_follow_up": True,
            "allow_schedule_suggestions": False,
        },
    )

    assert contextual["care_provenance"]["care_preference_version"] == 1
    assert contextual["care_provenance"]["profile_version"] is None
    assert contextual["care_context"]["profile_summary"][
        "recovery_preference"
    ] is None
    assert "短暂散步" not in contextual["message"]


@pytest.mark.parametrize(
    ("preference", "expected", "normalized_preference"),
    [
        ("micro_break", "micro_break", "micro_break"),
        ("task_decomposition", "workload_decomposition", "priority_review"),
        ("transition_buffer", "micro_break", "micro_break"),
    ],
)
def test_soft_support_preferences_apply_bounded_candidate_boost(
    preference, expected, normalized_preference
):
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        _alert(current_events=["一项安排"], dominant_stressors=[]),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=[
            _event("single", "普通安排", "15:30", "17:00", event_type="task")
        ],
        calendar_degraded=False,
        recent_observation=None,
        profile=None,
        profile_version=None,
        care_preferences={
            "version": 1,
            "preferred_support_types": [preference],
            "allow_schedule_suggestions": False,
        },
    )

    assert contextual["care_plan"]["intervention_type"] == expected
    assert contextual["care_plan"]["preference_matched"] == normalized_preference
    if preference == "micro_break":
        assert contextual["care_plan"]["action_minutes"] <= 5
    assert contextual["care_plan"]["ranking_score"] <= 1.0


def test_schedule_adjustment_candidate_is_strictly_gated_by_hard_preference():
    care = CareMessageService("Asia/Shanghai")

    def contextual(allowed: bool):
        return care.contextualize_alert(
            _alert(),
            source="forecast_warning",
            local_date=TARGET,
            calendar_events=_calendar(),
            calendar_degraded=False,
            recent_observation=None,
            profile=None,
            profile_version=None,
            care_preferences={
                "version": 1,
                "preferred_support_types": [],
                "allow_schedule_suggestions": allowed,
            },
        )

    assert contextual(False)["care_plan"]["intervention_type"] == (
        "transition_buffer"
    )
    assert contextual(True)["care_plan"]["intervention_type"] == (
        "schedule_adjustment"
    )


def test_missing_calendar_state_and_preference_uses_audited_generic_fallback():
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        _alert(current_events=[], dominant_stressors=[]),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=[],
        calendar_degraded=True,
        recent_observation=None,
        profile={"model_params": {"S_star_init": 82.0, "gamma": 99.0}},
        profile_version=8,
    )

    assert contextual["care_plan"]["intervention_type"] == "generic_fallback"
    assert contextual["care_context"]["context_quality"] == "degraded"
    assert contextual["care_provenance"]["profile_version"] is None
    assert "S_star" not in contextual["message"]
    assert "gamma" not in contextual["message"]
    assert 80 <= len(contextual["message"]) <= 220


@pytest.mark.parametrize(
    ("calendar_events", "calendar_degraded", "recent_observation", "quality"),
    [
        (_calendar(), False, _observation(energy=3.0, stress=8.0), "full"),
        (_calendar(), True, None, "partial"),
        ([], True, None, "degraded"),
    ],
)
def test_tier_three_severity_is_preserved_at_every_context_quality(
    calendar_events,
    calendar_degraded,
    recent_observation,
    quality,
):
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        _alert(
            tier=3,
            care_action="pause_and_seek_support",
            current_events=[] if not calendar_events else ["高等数学"],
            dominant_stressors=[],
        ),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=calendar_events,
        calendar_degraded=calendar_degraded,
        recent_observation=recent_observation,
        profile=None,
        profile_version=None,
    )

    assert contextual["care_context"]["context_quality"] == quality
    assert contextual["care_plan"]["intervention_type"] == (
        "pause_and_seek_support"
    )
    assert contextual["care_plan"]["template_id"] == "pause-and-support-v1"
    assert "暂停手头任务" in contextual["message"]
    if quality == "degraded":
        assert contextual["care_plan"]["facts_used"] == ("risk_window",)
        assert "当前可用上下文较少" in contextual["message"]
        assert "高等数学" not in contextual["message"]


def test_tier_one_degraded_context_still_uses_generic_fallback():
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        _alert(current_events=[], dominant_stressors=[]),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=[],
        calendar_degraded=True,
        recent_observation=None,
        profile=None,
        profile_version=None,
    )

    assert contextual["care_context"]["context_quality"] == "degraded"
    assert contextual["care_plan"]["intervention_type"] == "generic_fallback"


def test_unreviewed_profile_preference_is_never_injected_into_message():
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        _alert(),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=_calendar(),
        calendar_degraded=False,
        recent_observation=_observation(energy=2.0),
        profile={"care_preferences": {"recovery_preference": "服用处方药物"}},
        profile_version=9,
    )

    assert "处方" not in contextual["message"]
    assert "药物" not in contextual["message"]
    assert contextual["care_provenance"]["profile_version"] is None


def test_reviewed_preference_is_a_real_second_fact_when_calendar_is_empty():
    contextual = CareMessageService("Asia/Shanghai").contextualize_alert(
        _alert(current_events=[], dominant_stressors=[]),
        source="forecast_warning",
        local_date=TARGET,
        calendar_events=[],
        calendar_degraded=False,
        recent_observation=None,
        profile={"care_preferences": {"recovery_preference": "我喜欢散步"}},
        profile_version=10,
    )

    assert contextual["care_context"]["context_quality"] == "partial"
    assert contextual["care_provenance"]["profile_version"] == 10
    assert "恢复偏好" in contextual["message"]
    assert "短暂散步" in contextual["message"]
    assert 80 <= len(contextual["message"]) <= 220


def test_forecast_warning_persists_context_message_and_complete_provenance():
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create("CARE-CONTEXT")

    class Calendar:
        async def get_events(self, *_args):
            return _calendar()

    class Prediction:
        class Model:
            MODEL_VERSION = "care-context-model-v1"

        model = Model()

        def calculate(self, **_kwargs):
            return {
                "trajectory": [
                    {"time": "16:00", "stress_0_10": 6.8},
                    {"time": "16:30", "stress_0_10": 7.5},
                ],
                "alerts": [_alert(message=OLD_TIER_ONE)],
            }

    warnings = warning_repository(database)
    coordinator = ForecastCoordinator(
        participants=participants,
        profiles=ProfileRepository(database),
        observations=ObservationRepository(database),
        calendar=Calendar(),
        calendar_snapshots=CalendarSnapshotRepository(database),
        semantics=EventSemanticPreprocessor(
            EventSemanticCacheRepository(database),
            client=None,
            model="rules-only",
        ),
        prediction=Prediction(),
        forecasts=ForecastSnapshotRepository(database),
        warnings=warnings,
        timezone_name="Asia/Shanghai",
    )

    result = asyncio.run(
        coordinator.ensure_forecast(participant.id, TARGET, "care-context-test")
    )
    selected = result["output"]["selected_warning_candidates"][0]

    assert selected["current_events"] == ["高等数学"]
    assert selected["dominant_stressors"] == ["高等数学", "项目讨论"]
    assert selected["message"] != OLD_TIER_ONE
    assert selected["care_plan"]["intervention_type"] == "transition_buffer"
    assert "高等数学" in selected["message"]

    with database.session() as session:
        row = session.query(WarningSchedule).one()
        payload = dict(row.payload_json)
        provenance = dict(payload["care_provenance"])
        assert provenance["source_warning_id"] == str(row.id)
        assert provenance["source_forecast_id"] == str(row.forecast_id)
        assert provenance["forecast_version"] == row.forecast_version
        assert provenance["calendar_context_ids"] == ["math", "project"]


def test_user_requested_support_reuses_context_policy_and_templates(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2030, 1, 15, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    class Coordinator:
        care_messages = CareMessageService("Asia/Shanghai")

        async def ensure_forecast(self, *_args, **_kwargs):
            return {
                "id": "forecast-support",
                "forecast_version": "forecast-support-v1",
                "calendar_degraded": False,
                "calendar_events": _calendar(),
                "output": {"alerts": [_alert()]},
            }

    class Profiles:
        def current(self, _participant_id):
            return {
                "version": 3,
                "profile": {
                    "care_preferences": {"recovery_preference": "短暂散步"}
                },
            }

    class Observations:
        def recent_before(self, _participant_id, **_kwargs):
            return [_observation(energy=2.0)]

    monkeypatch.setattr("app.tools.care.datetime", FixedDateTime)
    tools = CareTools(
        profiles=Profiles(),
        observations=Observations(),
        calendar=None,
        tokens=None,
        timezone_name="Asia/Shanghai",
        forecast_coordinator=Coordinator(),
    )
    context = AgentContext(
        participant_id=uuid.uuid4(),
        participant_code="P",
        open_id="open",
        chat_id="chat",
        message_id="message",
        agent_run_id=uuid.uuid4(),
    )

    support = asyncio.run(tools.get_support(context, {"context": "有点累"}))

    assert support["support_type"] == "recovery"
    assert support["care_context"]["source"] == "user_requested_support"
    assert support["care_provenance"]["source_forecast_id"] == "forecast-support"
    assert "高等数学" in support["suggestion"]
    assert "短暂散步" in support["suggestion"]
