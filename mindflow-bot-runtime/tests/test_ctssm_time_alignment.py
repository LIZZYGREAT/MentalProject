import pytest

from mindflow_core.assessment import AssessmentModel
from utils.alert_monitor import AlertMonitor


def _prediction(*, observations=None):
    return AssessmentModel("Asia/Shanghai").predict(
        profile={},
        observations=list(observations or []),
        calendar_events=[],
        local_date="2030-01-15",
        initial_state={"stress_0_10": 6.0, "vitality_0_10": 3.0},
    )


def test_first_point_is_initial_state_and_terminal_is_separate_2400_state():
    result = _prediction()

    assert result.point_count == 288
    assert result.trajectory[0]["time"] == "00:00"
    assert result.trajectory[0]["stress_0_10"] == 6.0
    assert result.trajectory[0]["delta_stress_0_10"] == 0.0
    assert result.trajectory[0]["delta_vitality_0_10"] == 0.0
    assert result.trajectory[-1]["time"] == "23:55"
    assert result.stress_0_10 != result.trajectory[-1]["stress_0_10"]

    first, second = result.trajectory[:2]
    assert second["delta_stress_0_10"] == pytest.approx(
        second["stress_0_10"] - first["stress_0_10"], abs=0.0002
    )
    assert second["delta_vitality_0_10"] == pytest.approx(
        second["vitality_0_10"] - first["vitality_0_10"], abs=0.0002
    )


def test_observation_is_assimilated_at_its_timestamp_before_next_propagation():
    baseline = _prediction()
    observed = _prediction(
        observations=[
            {
                "type": "checkin",
                "observed_at": "2030-01-15T09:00:00+08:00",
                "payload": {"stress_0_10": 8.0, "energy_0_10": 2.0},
            }
        ]
    )
    by_time = {point["time"]: point for point in observed.trajectory}
    baseline_by_time = {point["time"]: point for point in baseline.trajectory}

    assert by_time["08:55"] == baseline_by_time["08:55"]
    assert by_time["09:00"]["observation_assimilated"] is True
    assert by_time["09:00"]["stress_0_10"] > baseline_by_time["09:00"][
        "stress_0_10"
    ]
    assert by_time["09:05"]["stress_0_10"] > baseline_by_time["09:05"][
        "stress_0_10"
    ]


def test_interval_load_is_not_counted_before_it_happens():
    result = AssessmentModel("Asia/Shanghai").predict(
        profile={},
        observations=[],
        calendar_events=[
            {
                "id": "event-1",
                "summary": "项目答辩",
                "description": "高强度正式答辩",
                "event_type": "task",
                "task_type": "exam",
                "start_time": "2030-01-15T09:00:00+08:00",
                "end_time": "2030-01-15T10:00:00+08:00",
            }
        ],
        local_date="2030-01-15",
        initial_state={"stress_0_10": 6.0, "vitality_0_10": 3.0},
    )
    by_time = {point["time"]: point for point in result.trajectory}

    assert by_time["08:55"]["continuous_load_hours"] == 0.0
    assert by_time["09:00"]["continuous_load_hours"] == 0.0
    assert by_time["09:05"]["continuous_load_hours"] > 0.0


def test_alert_confirmation_counts_elapsed_time_not_number_of_points():
    monitor = AlertMonitor(
        {
            "S_star_init": 50.0,
            "time_step": 5.0,
            "alert_thresholds": {
                "yellow_stress": 70.0,
                "orange_stress": 90.0,
                "red_stress": 96.0,
                "recovery_stress": 62.0,
                "yellow_confirm_minutes": 40.0,
            },
        }
    )
    rows = [
        {
            "time": f"00:{minute:02d}",
            "S": 72.0,
            "V": 72.0,
            "F": 0.0,
            "state": "DAY_ACTIVE",
            "delta_S": 0.0,
        }
        for minute in range(0, 45, 5)
    ]

    alerts, _confidence = monitor.analyze(rows)

    assert [alert["time"] for alert in alerts] == ["00:40"]
