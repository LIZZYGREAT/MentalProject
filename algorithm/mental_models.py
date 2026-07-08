"""Psychological trait scoring helpers."""

from typing import Dict

STRATEGY_RESILIENCE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "f_strategy": {
        "dull": 0.3,
        "saturated": 0.2,
        "sensitive": -0.3,
        "batterydrain": -0.2,
    },
    "C_strategy": {
        "low": 0.2,
        "threshold": 0.1,
        "high": -0.2,
    },
    "night_strategy": {
        "deep": 0.3,
        "normal": 0.0,
        "anxious": -0.3,
    },
    "rest_strategy": {
        "relieved": 0.2,
        "warmup": 0.1,
        "burnout": -0.1,
        "anxious": -0.2,
    },
}


def calculate_resilience_index(
    f_strategy: str,
    c_strategy: str,
    night_strategy: str,
    rest_strategy: str,
) -> float:
    """Map strategy choices to a bounded trait resilience score in [-1, 1]."""
    score = 0.0
    score += STRATEGY_RESILIENCE_WEIGHTS["f_strategy"].get(f_strategy, 0.0)
    score += STRATEGY_RESILIENCE_WEIGHTS["C_strategy"].get(c_strategy, 0.0)
    score += STRATEGY_RESILIENCE_WEIGHTS["night_strategy"].get(night_strategy, 0.0)
    score += STRATEGY_RESILIENCE_WEIGHTS["rest_strategy"].get(rest_strategy, 0.0)
    return max(-1.0, min(1.0, score))

