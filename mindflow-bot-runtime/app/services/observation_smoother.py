"""Causal fixed-lag smoother used only for retrospective curve reconstruction."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from zoneinfo import ZoneInfo


def _minute(value: str) -> int:
    hour, minute = (int(part) for part in value[:5].split(":"))
    return hour * 60 + minute


class FixedLagObservationSmoother:
    VERSION = "fixed-lag-observation-v1"

    def __init__(self, window_minutes: int = 90):
        self.window_minutes = max(5, int(window_minutes))

    def smooth(
        self, curve: list[dict], observations: list[dict], *, timezone_name: str
    ) -> tuple[list[dict], dict]:
        result = deepcopy(curve)
        timezone_value = ZoneInfo(timezone_name)
        used: list[str] = []
        for observation in sorted(observations, key=lambda item: item.get("observed_at", "")):
            payload = dict(observation.get("payload") or {})
            if "stress_0_10" not in payload and "energy_0_10" not in payload:
                continue
            observed = datetime.fromisoformat(str(observation["observed_at"]).replace("Z", "+00:00"))
            local = observed.astimezone(timezone_value)
            center = local.hour * 60 + local.minute
            event_activation = 1.0 if payload.get("event_ongoing") else (
                0.5 if payload.get("stress_event_since_last") else 0.0
            )
            target_index = min(range(len(result)), key=lambda i: abs(_minute(result[i]["time"]) - center))
            base_point = result[target_index]
            interval = base_point.get("stress_interval_90_0_10") or {}
            uncertainty = max(0.0, float(interval.get("upper", 0)) - float(interval.get("lower", 0))) / 10
            trust = min(0.85, 0.52 + 0.16 * uncertainty + 0.12 * event_activation)
            stress_residual = (
                float(payload["stress_0_10"]) - float(base_point.get("stress_0_10", 0))
                if "stress_0_10" in payload else None
            )
            energy_residual = (
                float(payload["energy_0_10"]) - float(base_point.get("vitality_0_10", 0))
                if "energy_0_10" in payload else None
            )
            tau = max(10.0, self.window_minutes / 3)
            for point in result:
                delta = center - _minute(point["time"])
                if delta < 0 or delta > self.window_minutes:
                    continue
                weight = trust * math.exp(-delta / tau)
                if stress_residual is not None:
                    point["stress_0_10"] = round(
                        min(10.0, max(0.0, float(point.get("stress_0_10", 0)) + weight * stress_residual)), 3
                    )
                if energy_residual is not None:
                    point["vitality_0_10"] = round(
                        min(10.0, max(0.0, float(point.get("vitality_0_10", 0)) + weight * energy_residual)), 3
                    )
            used.append(str(observation.get("id") or ""))
        return result, {
            "version": self.VERSION,
            "window_minutes": self.window_minutes,
            "observation_ids": used,
        }
