from datetime import datetime
from types import SimpleNamespace

from calibration.trajectory_validation import build_event_trajectory_diagnostics


def test_event_diagnostic_formats_clock_and_uses_exit_state_at_event_end():
    event = SimpleNamespace(
        event_id="event-1",
        name="exam",
        start_time=datetime(2030, 1, 15, 9, 0),
        end_time=datetime(2030, 1, 15, 10, 0),
    )
    assessment = SimpleNamespace(
        event_type="task",
        stress_intensity=0.8,
        task_demand=0.8,
        appraisal_observed=False,
        semantic={"values": {"difficulty": 0.9}},
    )
    rows = [
        {
            "time": f"{9 + minute // 60:02d}:{minute % 60:02d}",
            "S": float(index),
            "stress_equilibrium": float(index),
        }
        for index, minute in enumerate(range(0, 65, 5))
    ]

    diagnostic = build_event_trajectory_diagnostics(
        rows, [event], {"event-1": assessment}
    )["event-1"]

    assert diagnostic["time"] == "09:00-10:00"
    assert diagnostic["stress_in_event_last"] == 11.0
    assert diagnostic["stress_end"] == 12.0
    assert diagnostic["end_change"] == 12.0
