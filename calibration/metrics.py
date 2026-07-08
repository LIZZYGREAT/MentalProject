"""Curve evaluation metrics for daily feedback anchors."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from algorithm.time_utils import time_to_minutes


ANCHORS = (
    ("stress_morning", "energy_morning", "08:00"),
    ("stress_noon", "energy_noon", "13:00"),
    ("stress_evening", "energy_evening", "22:00"),
)


def normalize_rating(value: Any) -> Optional[float]:
    """Normalize feedback to a 0-100 model scale.

    Values in [0, 10] are treated as lightweight questionnaire ratings.
    Values above 10 are assumed to already use the model's 0-100 scale.
    """

    if value is None or value == "":
        return None
    val = float(value)
    if 0.0 <= val <= 10.0:
        val *= 10.0
    return max(0.0, min(100.0, val))


def evaluate_simulation(
    results: List[Dict[str, Any]],
    alerts: Iterable[Dict[str, Any]],
    feedback: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare a simulated curve with one day's user feedback."""

    if not results:
        return {
            "sample_count": 1,
            "anchor_count": 0,
            "stress_mae": None,
            "energy_mae": None,
            "trend_accuracy": None,
            "peak_time_error_min": None,
            "alert_score": None,
            "total_loss": 100.0,
        }

    stress_errors: List[float] = []
    energy_errors: List[float] = []
    predicted_anchor_pairs: List[Tuple[float, float]] = []
    feedback_anchor_pairs: List[Tuple[float, float]] = []

    for stress_key, energy_key, default_time in ANCHORS:
        anchor_time = feedback.get(stress_key.replace("stress_", "") + "_time", default_time)
        row = closest_result_at(results, anchor_time)
        if row is None:
            continue

        target_s = normalize_rating(feedback.get(stress_key))
        target_e = normalize_rating(feedback.get(energy_key))
        if target_s is not None:
            stress_errors.append(abs(float(row.get("S", 0.0)) - target_s))
        if target_e is not None:
            energy_errors.append(abs(float(row.get("E", 0.0)) - target_e))
        if target_s is not None and target_e is not None:
            predicted_anchor_pairs.append((float(row.get("S", 0.0)), float(row.get("E", 0.0))))
            feedback_anchor_pairs.append((target_s, target_e))

    stress_mae = _mean(stress_errors)
    energy_mae = _mean(energy_errors)
    trend_accuracy = _trend_accuracy(predicted_anchor_pairs, feedback_anchor_pairs)
    peak_error = _peak_time_error(results, feedback)
    alert_score = _alert_score(alerts, feedback)

    total_loss = calculate_total_loss(
        stress_mae=stress_mae,
        energy_mae=energy_mae,
        trend_accuracy=trend_accuracy,
        peak_time_error_min=peak_error,
        alert_score=alert_score,
    )

    return {
        "sample_count": 1,
        "anchor_count": len(stress_errors) + len(energy_errors),
        "stress_mae": stress_mae,
        "energy_mae": energy_mae,
        "trend_accuracy": trend_accuracy,
        "peak_time_error_min": peak_error,
        "alert_score": alert_score,
        "total_loss": total_loss,
    }


def aggregate_metrics(metrics_list: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    metrics = list(metrics_list)
    if not metrics:
        return {"sample_count": 0, "total_loss": 100.0}

    aggregated = {
        "sample_count": len(metrics),
        "anchor_count": sum(m.get("anchor_count", 0) or 0 for m in metrics),
        "stress_mae": _mean_present(m.get("stress_mae") for m in metrics),
        "energy_mae": _mean_present(m.get("energy_mae") for m in metrics),
        "trend_accuracy": _mean_present(m.get("trend_accuracy") for m in metrics),
        "peak_time_error_min": _mean_present(m.get("peak_time_error_min") for m in metrics),
        "alert_score": _mean_present(m.get("alert_score") for m in metrics),
    }
    aggregated["total_loss"] = calculate_total_loss(
        stress_mae=aggregated["stress_mae"],
        energy_mae=aggregated["energy_mae"],
        trend_accuracy=aggregated["trend_accuracy"],
        peak_time_error_min=aggregated["peak_time_error_min"],
        alert_score=aggregated["alert_score"],
    )
    return aggregated


def calculate_total_loss(
    stress_mae: Optional[float],
    energy_mae: Optional[float],
    trend_accuracy: Optional[float],
    peak_time_error_min: Optional[float],
    alert_score: Optional[float],
) -> float:
    """Weighted calibration objective; lower is better."""

    loss = 0.0
    active_weight = 0.0

    if stress_mae is not None:
        loss += 0.42 * stress_mae
        active_weight += 0.42
    if energy_mae is not None:
        loss += 0.28 * energy_mae
        active_weight += 0.28
    if trend_accuracy is not None:
        loss += 0.15 * (1.0 - trend_accuracy) * 35.0
        active_weight += 0.15
    if peak_time_error_min is not None:
        loss += 0.10 * min(35.0, peak_time_error_min / 3.0)
        active_weight += 0.10
    if alert_score is not None:
        loss += 0.05 * (1.0 - alert_score) * 35.0
        active_weight += 0.05

    if active_weight <= 0:
        return 100.0
    return round(loss / active_weight, 4)


def closest_result_at(results: List[Dict[str, Any]], time_str: str) -> Optional[Dict[str, Any]]:
    target = time_to_minutes(time_str)
    return min(
        results,
        key=lambda row: abs(time_to_minutes(row.get("time", "00:00")) - target),
        default=None,
    )


def _peak_time_error(results: List[Dict[str, Any]], feedback: Dict[str, Any]) -> Optional[float]:
    peak_time = feedback.get("stress_peak_time")
    if not peak_time:
        return None
    peak_row = max(results, key=lambda row: row.get("S", 0.0), default=None)
    if peak_row is None:
        return None
    return _minute_distance(peak_row.get("time", "00:00"), peak_time)


def _alert_score(alerts: Iterable[Dict[str, Any]], feedback: Dict[str, Any]) -> Optional[float]:
    expected = feedback.get("expected_alert_level")
    if expected is None or expected == "":
        return None
    expected_level = _parse_alert_level(expected)
    actual_level = 0
    for alert in alerts:
        actual_level = max(actual_level, _parse_alert_level(alert.get("type", "")))
        if "C" in alert:
            actual_level = max(actual_level, round(float(alert["C"]) * 3))
    return max(0.0, 1.0 - abs(actual_level - expected_level) / 3.0)


def _parse_alert_level(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, min(3, int(round(float(value)))))
    text = str(value).lower()
    if "红" in text or "red" in text:
        return 3
    if "橙" in text or "orange" in text:
        return 2
    if "黄" in text or "yellow" in text:
        return 1
    return 0


def _trend_accuracy(
    predicted_pairs: List[Tuple[float, float]],
    feedback_pairs: List[Tuple[float, float]],
    deadband: float = 3.0,
) -> Optional[float]:
    if len(predicted_pairs) < 2 or len(feedback_pairs) < 2:
        return None

    correct = 0
    total = 0
    for idx in range(1, len(predicted_pairs)):
        pred_s_delta = predicted_pairs[idx][0] - predicted_pairs[idx - 1][0]
        target_s_delta = feedback_pairs[idx][0] - feedback_pairs[idx - 1][0]
        pred_e_delta = predicted_pairs[idx][1] - predicted_pairs[idx - 1][1]
        target_e_delta = feedback_pairs[idx][1] - feedback_pairs[idx - 1][1]
        for pred_delta, target_delta in ((pred_s_delta, target_s_delta), (pred_e_delta, target_e_delta)):
            total += 1
            if _signed_bucket(pred_delta, deadband) == _signed_bucket(target_delta, deadband):
                correct += 1
    if total == 0:
        return None
    return correct / total


def _signed_bucket(value: float, deadband: float) -> int:
    if value > deadband:
        return 1
    if value < -deadband:
        return -1
    return 0


def _minute_distance(left: str, right: str) -> float:
    a = time_to_minutes(left)
    b = time_to_minutes(right)
    diff = abs(a - b)
    return float(min(diff, 1440 - diff))


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _mean_present(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [float(v) for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)
