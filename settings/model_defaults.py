"""Centralized defaults and symbolic constants for the local simulator.

This module is the single home for values that used to appear as implicit
fallbacks in unrelated modules. The runtime model parameters still live in
``entry.config.GLOBAL_DEFAULT_CONFIG``; these constants describe application
boundaries, state names, event classes, and compatibility aliases.
"""

from typing import Any, Dict, Tuple, Union

BASE_DATA_DIR = "data"
USER_CONFIG_DIR_NAME = "user_configs"
CALENDAR_DATA_DIR_NAME = "calendar_data"
STRESS_RECORDS_FILE = "stress_records.json"
USER_TOKEN_FILE = "user_token.json"
CALENDAR_INFO_FILE = "calendar_info.json"

DEFAULT_USER_ID = "default"
DEFAULT_DATE_FORMAT = "%Y-%m-%d"
DEFAULT_TIME_FORMAT = "%H:%M"

DEFAULT_WAKE_TIME = "07:30"
DEFAULT_SLEEP_TIME = "23:30"
DEFAULT_EVENT_START = "08:00"
DEFAULT_EVENT_END = "09:00"
DEFAULT_UNKNOWN_EVENT_NAME = "未知事件"

DEFAULT_TIME_STEP_MINUTES = 5
DEFAULT_RANDOM_SEED = 42
DEFAULT_INITIAL_STRESS = 50.0
DEFAULT_STRESS_THRESHOLD = 100.0
DEFAULT_INITIAL_ENERGY = 100.0
DEFAULT_ENERGY_CRITICAL = 20.0

APP_DEFAULT_HOST = "127.0.0.1"
APP_DEFAULT_PORT = 5000
DEFAULT_CALLBACK_PATH = "/callback"
FEISHU_REQUEST_TIMEOUT_SECONDS = 15.0
CACHE_EXPIRY_SECONDS = 300
TOKEN_EXPIRY_BUFFER_SECONDS = 300

HIGH_LOAD_EVENT_TYPES = ("course", "task", "gym", "library")
ROUTINE_EVENT_TYPES = ("meal", "nap", "sleep", "rest")
SLEEP_EVENT_TYPES = ("sleep",)
RECOVERY_STATES = ("RECOVERY_SLEEP", "NIGHT_SLEEP")
ACTIVE_NIGHT_STATES = ("LATE_NIGHT_ACTIVE", "NIGHT_OVERTIME")
RESTORATIVE_STATES = ("RECOVERY_SLEEP", "NIGHT_SLEEP", "ROUTINE_MAINTENANCE")

STRESS_MIN = 0.0
STRESS_MAX = 100.0
ENERGY_MIN = 0.0
ENERGY_MAX = 100.0
STRESS_FLOOR_MARGIN = 5.0
MIN_EVENT_DURATION_MINUTES = 5.0
MIN_PLOT_Y_RANGE = 10.0

DEFAULT_COURSE_PROFILE = {
    "credits": 2.5,
    "hours": 60.0,
    "level": "C",
}

DEFAULT_TASK_TYPE = "general"
DEFAULT_TASK_WEIGHT = 0.85

# Compatibility aliases keep older callers working while the public config uses
# clearer names. Values are resolved by ``settings.parameter_store.get_param``.
ParamAlias = Union[str, Tuple[str, str]]
PARAM_ALIASES: Dict[str, ParamAlias] = {
    "base_task_drain": "task_base_drain",
    "fatigue_acceleration_k": "fatigue_acceleration",
    "gym_epoc_rate": ("event_gym", "epoc_rate"),
}

PARAM_FALLBACKS: Dict[str, Any] = {
    "time_step": DEFAULT_TIME_STEP_MINUTES,
    "random_seed": DEFAULT_RANDOM_SEED,
    "S_star_init": DEFAULT_INITIAL_STRESS,
    "S_threshold": DEFAULT_STRESS_THRESHOLD,
    "E_critical": DEFAULT_ENERGY_CRITICAL,
    "default_wake_time": DEFAULT_WAKE_TIME,
    "default_sleep_time": DEFAULT_SLEEP_TIME,
    "course_base_drain": 5.5,
    "task_base_drain": 5.0,
    "fatigue_acceleration": 0.15,
    "K_resilience": 1.0,
    "Z_awake": 0.5,
    "Z_factor": 0.5,
    "D_t_course": 0.80,
    "D_t_task": 0.55,
}
