"""Deterministic Anchor + Smooth Residual Kernel daily-review reconstruction."""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any


PEAK_PERIODS: dict[str, tuple[int, int] | None] = {
    "overnight": (0, 360),
    "early_morning": (360, 540),
    "morning": (540, 720),
    "noon": (720, 840),
    "afternoon": (840, 1080),
    "evening": (1080, 1320),
    "late_night": (1320, 1440),
    "unknown": None,
}


def _minute(value: str) -> int:
    hour, minute = (int(part) for part in str(value)[:5].split(":"))
    return hour * 60 + minute


def _nearest(curve: list[dict], minute: int) -> int:
    return min(range(len(curve)), key=lambda i: abs(_minute(curve[i]["time"]) - minute))


def _gaussian(delta: float, sigma: float) -> float:
    return math.exp(-0.5 * (delta / max(1.0, sigma)) ** 2)


class RetrospectiveReconstructor:
    ALGORITHM_VERSION = "anchor-residual-kernel-v3"

    def __init__(
        self, *, morning_sigma: float = 90, end_sigma: float = 60,
        peak_rise: float = 120, peak_decay: float = 90,
        max_delta_per_5_min: float = 0.35, end_state_gain: float = 0.35,
    ):
        self.morning_sigma = morning_sigma
        self.end_sigma = end_sigma
        self.peak_rise = peak_rise
        self.peak_decay = peak_decay
        self.max_delta = max_delta_per_5_min
        self.end_state_gain = end_state_gain

    def reconstruct(
        self,
        curve: list[dict],
        review: dict[str, Any],
        *,
        source_terminal_state: dict[str, float] | None,
        end_anchor_minute: int,
        end_anchor_source: str,
        review_local_date: str,
        submitted_local_date: str,
    ) -> tuple[list[dict], dict, dict]:
        if not curve:
            raise ValueError("source forecast curve is empty")
        if not 0 <= end_anchor_minute < 24 * 60:
            raise ValueError("end anchor minute must be within the review day")
        if end_anchor_source not in {
            "same_day_submission", "scheduled_review_time"
        }:
            raise ValueError("end anchor source is invalid")
        base = deepcopy(curve)
        result = deepcopy(curve)
        end_minute = end_anchor_minute
        wake_minute = self._wake_minute(review)
        peak_minute, peak_reason = self._peak_minute(
            base, review["peak_period"], end_anchor_minute=end_minute
        )

        anchors = [
            ("start_stress", wake_minute, float(review["start_stress"]), "stress_0_10", self.morning_sigma, "gaussian"),
            ("peak_stress", peak_minute, float(review["peak_stress"]), "stress_0_10", 0.0, "peak"),
            ("end_stress", end_minute, float(review["end_stress"]), "stress_0_10", self.end_sigma, "gaussian"),
            ("start_energy", wake_minute, float(review["start_energy"]), "vitality_0_10", self.morning_sigma, "gaussian"),
            ("end_energy", end_minute, float(review["end_energy"]), "vitality_0_10", self.end_sigma, "gaussian"),
        ]
        corrections = {"stress_0_10": [0.0] * len(result), "vitality_0_10": [0.0] * len(result)}
        # A small prior denominator keeps anchors soft while still bringing
        # the posterior close to a reported value at the anchor itself.
        normalization = {"stress_0_10": [0.25] * len(result), "vitality_0_10": [0.25] * len(result)}
        anchor_diagnostics: list[dict] = []
        for name, center, reported, field, sigma, kernel in anchors:
            index = _nearest(base, center)
            residual = reported - float(base[index].get(field, 0))
            for i, point in enumerate(base):
                delta = _minute(point["time"]) - center
                if kernel == "peak":
                    scale = self.peak_rise if delta < 0 else self.peak_decay
                    weight = math.exp(-abs(delta) / max(1.0, scale))
                else:
                    weight = _gaussian(delta, sigma)
                corrections[field][i] += weight * residual
                normalization[field][i] += weight
            anchor_diagnostics.append({
                "name": name, "time": base[index]["time"], "reported": reported,
                "source_value": float(base[index].get(field, 0)), "kernel": kernel,
            })
        for field in corrections:
            for i, point in enumerate(result):
                raw = float(base[i].get(field, 0)) + corrections[field][i] / normalization[field][i]
                point[field] = min(10.0, max(0.0, raw))

        limited = self._slope_limit(result, "stress_0_10")
        limited += self._slope_limit(result, "vitality_0_10")
        self._light_smooth(result, "stress_0_10")
        self._light_smooth(result, "vitality_0_10")
        self._slope_limit(result, "stress_0_10")
        self._slope_limit(result, "vitality_0_10")
        for point in result:
            point["stress_0_10"] = round(float(point["stress_0_10"]), 3)
            point["vitality_0_10"] = round(float(point["vitality_0_10"]), 3)

        end_index = _nearest(base, end_minute)
        remaining_minutes = max(0, 1440 - end_minute)
        persistence = math.exp(-remaining_minutes / 720)
        forward_terminal = None
        if source_terminal_state is not None:
            terminal_base = dict(source_terminal_state)
            forward_terminal = {
                "stress_0_10": round(min(10.0, max(0.0,
                    float(terminal_base["stress_0_10"])
                    + self.end_state_gain * persistence * (
                        float(review["end_stress"]) - float(base[end_index].get("stress_0_10", 0))
                    ))), 3),
                "vitality_0_10": round(min(10.0, max(0.0,
                    float(terminal_base["vitality_0_10"])
                    + self.end_state_gain * persistence * (
                        float(review["end_energy"]) - float(base[end_index].get("vitality_0_10", 0))
                    ))), 3),
                "source": "daily_review_end_state",
                "gain": self.end_state_gain,
            }
        peak_point = max(result, key=lambda point: float(point.get("stress_0_10", 0)))
        analysis = {
            "peak_stress": float(peak_point["stress_0_10"]),
            "peak_time": peak_point["time"],
            "curve_last_point_state": {
                "stress_0_10": result[-1]["stress_0_10"],
                "vitality_0_10": result[-1]["vitality_0_10"],
            },
            "terminal_state": (
                {
                    "stress_0_10": forward_terminal["stress_0_10"],
                    "vitality_0_10": forward_terminal["vitality_0_10"],
                }
                if forward_terminal is not None
                else None
            ),
            "forward_terminal_state": forward_terminal,
            "labels": {
                "forecast": "预测", "instant": "即时反馈",
                "daily_review": "回顾反馈", "posterior": "回顾估计",
            },
        }
        for diagnostic in anchor_diagnostics:
            field = "vitality_0_10" if "energy" in diagnostic["name"] else "stress_0_10"
            diagnostic["posterior_value"] = result[_nearest(result, _minute(diagnostic["time"]))][field]
        diagnostics = {
            "algorithm_version": self.ALGORITHM_VERSION,
            "anchors": anchor_diagnostics,
            "peak_anchor_reason": peak_reason,
            "peak_anchor_time": result[_nearest(result, peak_minute)]["time"],
            "wake_anchor_time": result[_nearest(result, wake_minute)]["time"],
            "end_anchor_time": result[_nearest(result, end_minute)]["time"],
            "end_anchor_minute": end_minute,
            "end_anchor_source": end_anchor_source,
            "submitted_local_date": submitted_local_date,
            "review_local_date": review_local_date,
            "slope_limit_applied_count": limited,
            "energy_consumption_diagnostic": float(review["energy_consumption"]),
            "energy_consumption_used_as_hard_anchor": False,
            "peak_used_as_current_state": False,
            "source_terminal_complete": source_terminal_state is not None,
        }
        return result, analysis, diagnostics

    @staticmethod
    def _wake_minute(review: dict[str, Any]) -> int:
        raw = (review.get("raw") or {}).get("predicted_wake_time")
        if raw:
            try:
                return _minute(str(raw))
            except (TypeError, ValueError):
                pass
        return 8 * 60

    @staticmethod
    def _peak_minute(
        curve: list[dict], period: str, *, end_anchor_minute: int
    ) -> tuple[int, str]:
        bounds = PEAK_PERIODS.get(period)
        candidates = curve
        reason = "forecast_drive_max_unknown_period"
        if bounds is not None:
            start, end = bounds
            candidates = [point for point in curve if start <= _minute(point["time"]) < end]
            reason = "forecast_drive_max_within_reported_period"
        else:
            # "Unknown" is not evidence that the peak happened at the reported
            # closing state. Avoid manufacturing that exact anchor.
            away_from_end_anchor = [
                point for point in candidates
                if abs(_minute(point["time"]) - end_anchor_minute) > 30
            ]
            if away_from_end_anchor:
                candidates = away_from_end_anchor
        if not candidates:
            candidates = curve
            reason = "forecast_drive_max_fallback"
        def drive(point: dict) -> float:
            interval = point.get("stress_interval_90_0_10") or {}
            uncertainty = max(0.0, float(interval.get("upper", 0)) - float(interval.get("lower", 0)))
            return (
                float(point.get("stress_0_10", 0))
                + 2 * float(point.get("event_stress_input", 0))
                + 0.2 * uncertainty
            )
        selected = max(candidates, key=drive)
        return _minute(selected["time"]), reason

    def _slope_limit(self, curve: list[dict], field: str) -> int:
        changed = 0
        for i in range(1, len(curve)):
            previous = float(curve[i - 1][field])
            current = float(curve[i][field])
            bounded = min(previous + self.max_delta, max(previous - self.max_delta, current))
            if bounded != current:
                curve[i][field] = bounded
                changed += 1
        for i in range(len(curve) - 2, -1, -1):
            following = float(curve[i + 1][field])
            current = float(curve[i][field])
            bounded = min(following + self.max_delta, max(following - self.max_delta, current))
            if bounded != current:
                curve[i][field] = bounded
                changed += 1
        return changed

    @staticmethod
    def _light_smooth(curve: list[dict], field: str) -> None:
        source = [float(point[field]) for point in curve]
        for i in range(1, len(curve) - 1):
            curve[i][field] = 0.2 * source[i - 1] + 0.6 * source[i] + 0.2 * source[i + 1]
