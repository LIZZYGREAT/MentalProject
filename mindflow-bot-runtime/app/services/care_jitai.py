"""Transparent Stage-6 JITAI scores and normalized intervention options."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
import math
from typing import Any, Mapping
from zoneinfo import ZoneInfo


CARE_JITAI_VERSION = "care-jitai.v1"
RECEPTIVITY_MODEL_VERSION = "receptivity-logistic-v1"
INTERVENTION_OPTIONS = (
    "brief_check_in",
    "micro_break",
    "protected_break",
    "priority_review",
    "hydration_movement",
    "social_support",
    "schedule_adjustment_suggestion",
)

_OPTION_MAP = {
    "brief_check_in": "brief_check_in",
    "generic_fallback": "brief_check_in",
    "micro_break": "micro_break",
    "transition_buffer": "micro_break",
    "protected_break": "protected_break",
    "priority_review": "priority_review",
    "workload_decomposition": "priority_review",
    "task_decomposition": "priority_review",
    "recovery": "hydration_movement",
    "hydration": "hydration_movement",
    "walk": "hydration_movement",
    "hydration_movement": "hydration_movement",
    "pause_and_seek_support": "social_support",
    "trusted_person": "social_support",
    "social_support": "social_support",
    "schedule_adjustment": "schedule_adjustment_suggestion",
    "schedule_adjustment_suggestion": "schedule_adjustment_suggestion",
}


def normalized_intervention_type(value: Any) -> str:
    return _OPTION_MAP.get(str(value or "").strip(), "brief_check_in")


def normalized_intervention_types(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        raise ValueError("intervention types must be a list")
    normalized: set[str] = set()
    for value in values:
        raw = str(value or "").strip()
        if raw not in _OPTION_MAP:
            raise ValueError(f"unsupported intervention type: {raw}")
        normalized.add(_OPTION_MAP[raw])
    return sorted(normalized)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: Any) -> float:
    return round(max(0.0, min(1.0, _number(value))), 4)


def _clock(value: Any) -> time | None:
    if value in (None, ""):
        return None
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        return None


def _in_quiet_hours(value: time, start: time | None, end: time | None) -> bool:
    if start is None or end is None or start == end:
        return False
    return start <= value < end if start < end else value >= start or value < end


@dataclass(frozen=True)
class JITAIDecision:
    schema_version: str
    receptivity_model_version: str
    option_type: str
    vulnerability_score: float
    receptivity_score: float
    decision_score: float
    decision_rule: str
    scheduled_at: str | None
    vulnerability_features: dict[str, float]
    receptivity_features: dict[str, float]
    vulnerability_decomposition: dict[str, float]
    receptivity_decomposition: dict[str, float]
    explanation_codes: tuple[str, ...]
    observational_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CareJITAIEngine:
    """Rule + logistic-receptivity policy with auditable decompositions."""

    VULNERABILITY_WEIGHTS = {
        "predicted_stress": 0.35,
        "stress_duration": 0.15,
        "workload": 0.15,
        "continuous_load": 0.10,
        "recent_ema": 0.10,
        "recovery_debt": 0.15,
    }
    RECEPTIVITY_COEFFICIENTS = {
        "intercept": -0.15,
        "time_of_day": 0.65,
        "event_interruptibility": 1.25,
        "stress_level": -0.25,
        "warning_level": 0.20,
        "previous_warning_interval": 0.90,
        "recent_dismissal": -1.45,
        "quiet_hours": -4.00,
        "preferred_window": 0.45,
    }

    def __init__(self, timezone_name: str):
        self.timezone = ZoneInfo(timezone_name)

    def decide(
        self,
        *,
        context: Any,
        alert: Mapping[str, Any],
        proposed_type: str,
        preferences: Mapping[str, Any] | None = None,
        history: Mapping[str, Any] | None = None,
    ) -> JITAIDecision:
        prefs = dict(preferences or {})
        recent = dict(history or {})
        risk_time = datetime.fromisoformat(str(context.risk_time))
        level = max(1, min(3, int(_number(context.warning_level, 1.0))))
        raw_predicted = _number(context.stress_0_10)
        predicted = _clamp(
            raw_predicted / 10.0
            if raw_predicted > 0
            else {1: 0.65, 2: 0.78, 3: 0.90}[level]
        )
        raw_duration = alert.get("continuous_hours")
        duration = _clamp(
            _number(raw_duration) / 4.0
            if raw_duration is not None
            else 0.5
        )
        has_workload = bool(
            context.current_events
            or context.dominant_stressors
            or context.active_event
            or context.next_event
        )
        workload = _clamp(
            alert.get("workload", 0.75 if has_workload else 0.0)
        )
        continuous_load = _clamp(
            alert.get("continuous_load_factor", duration)
        )
        observation = context.recent_observation or {}
        recent_ema = _clamp(
            _number(observation.get("stress_0_10"), context.stress_0_10) / 10.0
        )
        recovery_debt = _clamp(context.fatigue_0_1)
        vulnerability_features = {
            "predicted_stress": predicted,
            "stress_duration": duration,
            "workload": workload,
            "continuous_load": continuous_load,
            "recent_ema": recent_ema,
            "recovery_debt": recovery_debt,
        }
        vulnerability_decomposition = {
            name: round(value * self.VULNERABILITY_WEIGHTS[name], 4)
            for name, value in vulnerability_features.items()
        }
        vulnerability = _clamp(sum(vulnerability_decomposition.values()))

        local_clock = risk_time.astimezone(self.timezone).time().replace(tzinfo=None)
        quiet = _in_quiet_hours(
            local_clock,
            _clock(prefs.get("quiet_hours_start")),
            _clock(prefs.get("quiet_hours_end")),
        )
        active_type = str((context.active_event or {}).get("event_type") or "").casefold()
        interruptibility = 0.15 if active_type in {"course", "meeting", "exam"} else 0.35 if context.active_event else 0.90
        last_intervention_at = self._instant(recent.get("last_intervention_at"))
        last_dismissal_at = self._instant(recent.get("last_dismissal_at"))
        interval_minutes = (
            max(0.0, (risk_time - last_intervention_at).total_seconds() / 60.0)
            if last_intervention_at is not None and last_intervention_at <= risk_time
            else _number(recent.get("previous_warning_interval_minutes"), 1440.0)
        )
        recent_dismissal = (
            risk_time - timedelta(hours=24) <= last_dismissal_at <= risk_time
            if last_dismissal_at is not None
            else bool(recent.get("recent_dismissal"))
        )
        receptivity_features = {
            "time_of_day": 1.0 if 7 <= local_clock.hour < 22 else 0.0,
            "event_interruptibility": interruptibility,
            "stress_level": predicted,
            "warning_level": _clamp(level / 3.0),
            "previous_warning_interval": _clamp(interval_minutes / 240.0),
            "recent_dismissal": 1.0 if recent_dismissal else 0.0,
            "quiet_hours": 1.0 if quiet else 0.0,
            "preferred_window": 1.0
            if self._matches_preferred_window(
                local_clock, prefs.get("preferred_reminder_windows")
            )
            else 0.0,
        }
        logit = self.RECEPTIVITY_COEFFICIENTS["intercept"]
        receptivity_decomposition: dict[str, float] = {}
        for name, value in receptivity_features.items():
            contribution = self.RECEPTIVITY_COEFFICIENTS[name] * value
            receptivity_decomposition[name] = round(contribution, 4)
            logit += contribution
        tolerance = _clamp(prefs.get("interruption_tolerance", 0.5))
        logit += (tolerance - 0.5) * 0.8
        receptivity = _clamp(1.0 / (1.0 + math.exp(-logit)))
        decision_score = _clamp(vulnerability * receptivity)

        next_window: datetime | None = None
        if vulnerability >= 0.50 and receptivity < 0.45:
            next_window = self._next_acceptable_window(
                risk_time, context=context, preferences=prefs
            )
            rule = "next_acceptable_window" if next_window else "hold"
        elif decision_score >= 0.18:
            rule = "send_at_planned_time"
        else:
            rule = "hold"
        explanations = tuple(
            name
            for name, _value in sorted(
                vulnerability_decomposition.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
            if _value > 0
        )
        return JITAIDecision(
            schema_version=CARE_JITAI_VERSION,
            receptivity_model_version=RECEPTIVITY_MODEL_VERSION,
            option_type=normalized_intervention_type(proposed_type),
            vulnerability_score=vulnerability,
            receptivity_score=receptivity,
            decision_score=decision_score,
            decision_rule=rule,
            scheduled_at=next_window.isoformat() if next_window else None,
            vulnerability_features=vulnerability_features,
            receptivity_features=receptivity_features,
            vulnerability_decomposition=vulnerability_decomposition,
            receptivity_decomposition=receptivity_decomposition,
            explanation_codes=explanations,
        )

    def _next_acceptable_window(
        self,
        risk_time: datetime,
        *,
        context: Any,
        preferences: Mapping[str, Any],
    ) -> datetime | None:
        candidate = risk_time
        if context.active_event and context.active_event.get("end_time"):
            try:
                candidate = max(
                    candidate,
                    datetime.fromisoformat(str(context.active_event["end_time"])),
                )
            except ValueError:
                pass
        start = _clock(preferences.get("quiet_hours_start"))
        end = _clock(preferences.get("quiet_hours_end"))
        local = candidate.astimezone(self.timezone)
        if _in_quiet_hours(local.time().replace(tzinfo=None), start, end) and end:
            end_date = local.date()
            if start and start > end and local.time().replace(tzinfo=None) >= start:
                end_date += timedelta(days=1)
            candidate = datetime.combine(end_date, end, self.timezone)
        if candidate.date() != risk_time.astimezone(self.timezone).date():
            return None
        return candidate if candidate <= risk_time + timedelta(hours=3) else None

    @staticmethod
    def _instant(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None

    @staticmethod
    def _matches_preferred_window(
        local_clock: time, windows: Any
    ) -> bool:
        if not isinstance(windows, (list, tuple)):
            return False
        for value in windows[:12]:
            try:
                start_text, end_text = str(value).split("-", 1)
                start, end = time.fromisoformat(start_text), time.fromisoformat(end_text)
            except ValueError:
                continue
            if _in_quiet_hours(local_clock, start, end):
                return True
        return False
