"""Pure warning-candidate selection policy.

The prediction model may describe several pressure episodes.  This module is
the single, deterministic place that turns those episodes into at most two
proactive warning opportunities.  Durable delivery limits are enforced again
by :class:`WarningScheduleRepository`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar


def _minute(value: Any) -> int | None:
    try:
        hour, minute = (int(part) for part in str(value or "")[:5].split(":"))
    except (TypeError, ValueError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class WarningPolicy:
    POLICY_VERSION: ClassVar[str] = "warning-policy-v1"

    max_daily_sends: int = 2
    min_interval_minutes: int = 240

    def identity_payload(self) -> dict[str, object]:
        return {
            "policy_version": self.POLICY_VERSION,
            "max_daily_sends": self.max_daily_sends,
            "min_interval_minutes": self.min_interval_minutes,
        }

    @staticmethod
    def _episode_key(alert: dict[str, Any], fallback: int) -> str:
        explicit = alert.get("episode_identity") or alert.get("episode_index")
        if explicit is not None:
            return str(explicit)
        stressors = tuple(sorted(str(value) for value in alert.get("dominant_stressors") or []))
        events = tuple(sorted(str(value) for value in alert.get("current_events") or []))
        source = str(alert.get("trigger_source") or "trajectory_episode")
        return repr((stressors, events, source, fallback))

    @staticmethod
    def _priority(alert: dict[str, Any], minute: int) -> tuple[float, ...]:
        # Higher tier/risk/current stress/burden wins.  Earlier time is the
        # final tie-breaker only; chronological order is not priority.
        return (
            _number(alert.get("tier")),
            _number(alert.get("C")),
            _number(alert.get("S")),
            _number(alert.get("elevated_auc")),
            -float(minute),
        )

    def select_daily_candidates(self, alerts: Any) -> list[dict[str, Any]]:
        if self.max_daily_sends <= 0 or not isinstance(alerts, list):
            return []
        best_by_episode: dict[str, tuple[tuple[float, ...], int, dict[str, Any]]] = {}
        for index, raw in enumerate(alerts):
            if not isinstance(raw, dict):
                continue
            minute = _minute(raw.get("time"))
            if minute is None:
                continue
            key = self._episode_key(raw, index)
            priority = self._priority(raw, minute)
            current = best_by_episode.get(key)
            if current is None or priority > current[0]:
                best_by_episode[key] = (priority, minute, dict(raw))

        ranked = sorted(best_by_episode.values(), key=lambda item: item[0], reverse=True)
        selected: list[tuple[tuple[float, ...], int, dict[str, Any]]] = []
        for candidate in ranked:
            minute = candidate[1]
            if all(abs(minute - chosen[1]) >= self.min_interval_minutes for chosen in selected):
                selected.append(candidate)
                if len(selected) >= self.max_daily_sends:
                    break
        # Persist and display the chosen opportunities in delivery order.
        selected.sort(key=lambda item: item[1])
        return [
            {
                **item[2],
                "warning_priority": list(item[0]),
                "warning_policy": {
                    "max_daily_sends": self.max_daily_sends,
                    "min_interval_minutes": self.min_interval_minutes,
                },
            }
            for item in selected
        ]
