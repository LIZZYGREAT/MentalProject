"""Fast simulation runner for evaluation and calibration."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from entity.user import User
from utils.event_factory import EventFactory
from utils.routine_weaver import RoutineWeaver
from settings.model_defaults import (
    DEFAULT_INITIAL_ENERGY,
    DEFAULT_INITIAL_STRESS,
    DEFAULT_USER_ID,
)


def run_simulation_for_calibration(
    date_str: str,
    events_json: List[Dict[str, Any]],
    user_params: Optional[Dict[str, Any]] = None,
    yesterday_state: Optional[Dict[str, Any]] = None,
    weave_routines: bool = True,
) -> Dict[str, Any]:
    """Run one day without plotting or persistence side effects."""

    user = User(user_id=DEFAULT_USER_ID, params=user_params or {}, load_from_file=False)
    base_events = EventFactory.create_from_json(events_json or [])
    final_events = base_events
    if weave_routines:
        weaver = RoutineWeaver(user)
        if hasattr(weaver, "weave"):
            final_events = weaver.weave(base_events, date_str)
        else:
            final_events = weaver.inject_routine_events(base_events, date_str)

    if yesterday_state:
        prev_s = yesterday_state.get("S_end", user.get_current_S_star())
        prev_e = yesterday_state.get("E_end", DEFAULT_INITIAL_ENERGY)
        user.set_stress_baseline(
            yesterday_state.get("S_star", DEFAULT_INITIAL_STRESS),
            yesterday_state.get("S_threshold", user.get_current_threshold()),
        )
        user.set_sleep_debt(yesterday_state.get("sleep_debt", 0.0))
    else:
        prev_s = user.get_current_S_star()
        prev_e = DEFAULT_INITIAL_ENERGY

    result_tuple = user.solver.simulate_day(final_events, prev_s, prev_e, date_str)
    (
        results,
        end_s,
        end_e,
        wake_time,
        sleep_markers,
        alerts,
        confidence,
        logs,
        profiles,
        wake_s,
    ) = result_tuple

    return {
        "status": "success",
        "date": date_str,
        "results": results,
        "alerts": alerts,
        "confidence": confidence,
        "logs": logs,
        "event_profile": profiles,
        "wake_time": wake_time,
        "sleep_markers": sleep_markers,
        "wake_s": wake_s,
        "final_state": {
            "S_end": end_s,
            "E_end": end_e,
            "S_star": user.get_current_S_star(),
            "S_threshold": user.get_current_threshold(),
            "sleep_debt": user.get_sleep_debt(),
        },
    }
