from datetime import date
import hashlib
import json
from zoneinfo import ZoneInfo

from app.contracts.warning import WarningDeliveryPolicyConfig
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.warning_policy import WarningPolicy
from mindflow_core.assessment import AssessmentModel


def _sha(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_production_ctssm_fixture_is_stable_after_legacy_removal():
    high_semantics = {
        "values": {
            "difficulty": 1,
            "cognitive_demand": 1,
            "stakes": 1,
            "time_pressure": 1,
            "social_evaluation": 1,
            "uncontrollability": 1,
            "novelty": 1,
            "expected_effort": 1,
            "uncertainty": 1,
            "unfinished": 0,
        }
    }
    calendar = [
        {
            "id": "exam-1",
            "summary": "关键答辩",
            "description": "正式项目答辩",
            "event_type": "task",
            "task_type": "exam",
            "start_time": "2030-01-15T09:00:00+08:00",
            "end_time": "2030-01-15T14:00:00+08:00",
            "metadata": {"semantic": high_semantics},
        },
        {
            "id": "rest-1",
            "summary": "休息",
            "description": "",
            "event_type": "rest",
            "start_time": "2030-01-15T15:00:00+08:00",
            "end_time": "2030-01-15T15:30:00+08:00",
            "metadata": {
                "semantic": {
                    "values": {key: 0 for key in high_semantics["values"]}
                }
            },
        },
    ]
    result = AssessmentModel("Asia/Shanghai").predict(
        profile={},
        observations=[{
            "type": "checkin",
            "observed_at": "2030-01-15T10:07:00+08:00",
            "payload": {"stress_0_10": 8.2, "energy_0_10": 3.4},
        }],
        calendar_events=calendar,
        local_date="2030-01-15",
        initial_state={"stress_0_10": 6.0, "vitality_0_10": 3.0},
    )
    coordinator = object.__new__(ForecastCoordinator)
    coordinator.timezone = ZoneInfo("Asia/Shanghai")
    coordinator.warning_lead_minutes = 20
    coordinator.warning_late_grace_minutes = 10
    coordinator.warning_episode_drift_minutes = 15
    selected = WarningPolicy(
        WarningDeliveryPolicyConfig(2, 240)
    ).select_daily_candidates(list(result.alerts))
    warning_windows = [
        ForecastCoordinator._serializable_warning(item)
        for item in coordinator._warning_windows(selected, date(2030, 1, 15))
    ]

    assert result.model_version == "mindflow-ctssm-runtime-v7"
    assert result.model_family == "stress-ctssm.m0"
    assert result.point_count == 288
    assert _sha(result.trajectory) == (
        "c6f17e9b5bad3e674807ae1cc0a905746a940382a0cb55602222d78ac7805c6d"
    )
    assert _sha(result.alerts) == (
        "3b674b9753941b253047a42fc305efea884dd011e68e0a6239bb8315ea821a8c"
    )
    assert _sha(warning_windows) == (
        "d32b572cee8e70c37e5571265ff2b4c22b0cf34c9d4e9da8a86d25be15ad8bce"
    )
    assert _sha(result.confidence_series) == (
        "64a0bab1313af875ecef28e137f128dc98f27b254b159d03aa87ce1897e22c95"
    )
    assert (result.stress_0_10, result.vitality_0_10) == (4.98, 7.2)
    assert result.active_states == ("S",)
    assert result.alert_count == 2
    assert sum(point["observation_assimilated"] for point in result.trajectory) == 1
    assert len(warning_windows) == 1
