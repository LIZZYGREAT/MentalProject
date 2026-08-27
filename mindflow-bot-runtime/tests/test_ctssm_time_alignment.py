from mindflow_core.assessment import AssessmentModel


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
    assert result.trajectory[-1]["time"] == "23:55"
    assert result.stress_0_10 != result.trajectory[-1]["stress_0_10"]


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
