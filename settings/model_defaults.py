"""Centralized defaults and symbolic constants for the local simulator.

This module is the single home for values that used to appear as implicit
fallbacks in unrelated modules. The runtime model parameters live in
``entry.config.GLOBAL_DEFAULT_CONFIG``; these constants describe application
boundaries, state names and event classes.
"""

from typing import Any, Dict

DEFAULT_DATE_FORMAT = "%Y-%m-%d"
DEFAULT_TIME_FORMAT = "%H:%M"

DEFAULT_WAKE_TIME = "07:30"
DEFAULT_SLEEP_TIME = "23:30"
DEFAULT_EVENT_START = "08:00"
DEFAULT_EVENT_END = "09:00"
DEFAULT_UNKNOWN_EVENT_NAME = "未知事件"

DEFAULT_TIME_STEP_MINUTES = 5
DEFAULT_INITIAL_VITALITY = 72.0
# Compatibility alias: public payloads still expose ``E`` while the model and
# UI describe it as subjective vitality rather than a physiological reserve.
DEFAULT_INITIAL_ENERGY = DEFAULT_INITIAL_VITALITY
DEFAULT_ENERGY_CRITICAL = 25.0

HIGH_LOAD_EVENT_TYPES = ("course", "task", "gym", "library")
ROUTINE_EVENT_TYPES = ("meal", "nap", "sleep", "rest")
RECOVERY_STATES = ("RECOVERY_SLEEP", "NIGHT_SLEEP")
ACTIVE_NIGHT_STATES = ("LATE_NIGHT_ACTIVE", "NIGHT_OVERTIME")

MIN_EVENT_DURATION_MINUTES = 5.0
MIN_PLOT_Y_RANGE = 10.0

DEFAULT_COURSE_PROFILE = {
    "credits": 2.5,
    "hours": 60.0,
    "level": "C",
}

DEFAULT_TASK_TYPE = "general"

# Last-resort values for ``settings.parameter_store.get_param``.  They mirror
# ``GLOBAL_DEFAULT_CONFIG`` and only cover the keys the runtime reads; every one
# of them is present in the global config, so this map currently never changes
# an answer.  It is retained as a defensive net for sparse caller-supplied
# ``params`` and is locked by ``tests/test_parameter_store.py``.
#
# ``ctssm_params`` is deliberately absent: an empty-dict fallback would look
# harmless while silently dropping the whole CTSSM coefficient block.  Callers
# that read it pass their own ``{}`` default AND merge the global block.
PARAM_FALLBACKS: Dict[str, Any] = {
    "time_step": DEFAULT_TIME_STEP_MINUTES,
    "model_family": "stress-ctssm.m0",
    "E_critical": DEFAULT_ENERGY_CRITICAL,
}
