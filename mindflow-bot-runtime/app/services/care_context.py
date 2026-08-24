"""Bounded, factual context for proactive and user-requested care."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
import math
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo


CARE_CONTEXT_SCHEMA_VERSION = "care_context.v1"
_SPACE = re.compile(r"\s+")
_PREFERENCE_KEYS = {
    "recovery_preference": "recovery_preference",
    "known_recovery_preference": "recovery_preference",
    "preferred_recovery": "recovery_preference",
    "support_preference": "support_preference",
    "preferred_support": "support_preference",
    "care_preference": "care_preference",
}
_REVIEWED_PREFERENCES = {
    "recovery_preference": (
        ("散步", "短暂散步"),
        ("走动", "简单走动"),
        ("补水", "补水"),
        ("安静", "安静休息"),
        ("音乐", "听一会儿音乐"),
        ("伸展", "简单伸展"),
    ),
    "support_preference": (
        ("朋友", "联系信任的朋友"),
        ("家人", "联系信任的家人"),
        ("同学", "联系信任的同学"),
        ("老师", "联系信任的老师"),
        ("导师", "联系信任的导师"),
        ("可信任", "联系一位可信任的人"),
    ),
    "care_preference": (
        ("简短", "简短提醒"),
        ("温和", "温和提醒"),
    ),
}


def _text(value: Any, limit: int) -> str:
    return _SPACE.sub(" ", str(value or "")).strip()[:limit]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _reviewed_preference(value: Any, kind: str) -> str | None:
    text_value = _text(value, 80)
    for token, reviewed in _REVIEWED_PREFERENCES.get(kind, ()):
        if token in text_value:
            return reviewed
    return None


def _zero_to_ten(value: Any) -> float:
    number = _number(value)
    if number > 10.0:
        number /= 10.0
    return round(max(0.0, min(number, 10.0)), 3)


def _parse_datetime(
    value: Any,
    *,
    local_date: date,
    timezone_value: ZoneInfo,
) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text_value = str(value or "").strip()
        if not text_value:
            return None
        if len(text_value) in {5, 8} and text_value[2:3] == ":":
            try:
                parsed_time = time.fromisoformat(text_value)
            except ValueError:
                return None
            parsed = datetime.combine(local_date, parsed_time)
        else:
            try:
                parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_value)
    return parsed.astimezone(timezone_value)


@dataclass(frozen=True)
class CareProfileSummary:
    recent_stress_tendency: str | None
    recent_energy_tendency: str | None
    recovery_preference: str | None
    support_preference: str | None
    care_preference: str | None
    profile_version: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CareContext:
    schema_version: str
    source: str
    local_date: str
    risk_time: str
    warning_level: str
    trigger_source: str
    stress_0_10: float
    vitality_0_10: float
    fatigue_0_1: float
    current_events: tuple[str, ...]
    dominant_stressors: tuple[str, ...]
    previous_event: dict[str, Any] | None
    active_event: dict[str, Any] | None
    next_event: dict[str, Any] | None
    recent_observation: dict[str, Any] | None
    profile_summary: CareProfileSummary
    care_action: str
    calendar_degraded: bool
    context_quality: str
    fact_codes: tuple[str, ...]
    profile_fact_used: bool
    care_preference_version: int | None
    allow_follow_up: bool

    @property
    def calendar_context_ids(self) -> tuple[str, ...]:
        return tuple(
            str(event["id"])
            for event in (self.previous_event, self.active_event, self.next_event)
            if event and event.get("id")
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["profile_summary"] = self.profile_summary.to_dict()
        result["calendar_context_ids"] = list(self.calendar_context_ids)
        return result


class CareContextBuilder:
    def __init__(self, timezone_name: str):
        self.timezone = ZoneInfo(timezone_name)

    def build(
        self,
        *,
        source: str,
        local_date: date,
        alert: Mapping[str, Any],
        calendar_events: list[Mapping[str, Any]],
        calendar_degraded: bool,
        recent_observation: Mapping[str, Any] | None,
        profile: Mapping[str, Any] | None,
        profile_version: int | None,
        care_preferences: Mapping[str, Any] | None = None,
    ) -> CareContext:
        risk_time = self._risk_time(alert, local_date)
        previous_event, active_event, next_event = self._calendar_neighbours(
            calendar_events,
            local_date=local_date,
            risk_time=risk_time,
        )
        observation = self._observation(recent_observation, risk_time)
        profile_summary = self._profile_summary(
            profile,
            profile_version=profile_version,
            observation=observation,
            care_preferences=care_preferences,
        )
        current_events = self._bounded_list(alert.get("current_events"))
        dominant_stressors = self._bounded_list(alert.get("dominant_stressors"))
        warning_level = _text(
            alert.get("tier") or alert.get("intensity_zone") or "1", 32
        )
        profile_fact_used = bool(
            profile_summary.recovery_preference
            or profile_summary.care_preference
            or (
                profile_summary.support_preference
                and (
                    warning_level.casefold() in {"3", "red", "critical"}
                    or _text(alert.get("care_action"), 64)
                    == "pause_and_seek_support"
                )
            )
        )

        fact_codes = ["risk_window"]
        if any((previous_event, active_event, next_event, current_events, dominant_stressors)):
            fact_codes.append("calendar_or_workload")
        if observation or profile_fact_used:
            fact_codes.append("recent_state_or_preference")
        context_quality = (
            "full"
            if len(fact_codes) == 3 and not calendar_degraded
            else "partial"
            if len(fact_codes) >= 2
            else "degraded"
        )
        try:
            preference_version_value = int(
                (care_preferences or {}).get("version") or 0
            )
        except (TypeError, ValueError):
            preference_version_value = 0
        return CareContext(
            schema_version=CARE_CONTEXT_SCHEMA_VERSION,
            source=_text(source, 48) or "forecast_warning",
            local_date=local_date.isoformat(),
            risk_time=risk_time.isoformat(),
            warning_level=warning_level,
            trigger_source=_text(
                alert.get("trigger_source") or "trajectory_episode", 64
            ),
            stress_0_10=_zero_to_ten(alert.get("S", alert.get("stress_0_10"))),
            vitality_0_10=_zero_to_ten(
                alert.get("V", alert.get("E", alert.get("vitality_0_10")))
            ),
            fatigue_0_1=round(
                max(0.0, min(_number(alert.get("F", alert.get("fatigue_0_1"))), 1.0)),
                3,
            ),
            current_events=current_events,
            dominant_stressors=dominant_stressors,
            previous_event=previous_event,
            active_event=active_event,
            next_event=next_event,
            recent_observation=observation,
            profile_summary=profile_summary,
            care_action=_text(alert.get("care_action") or "brief_check_in", 64),
            calendar_degraded=bool(calendar_degraded),
            context_quality=context_quality,
            fact_codes=tuple(fact_codes),
            profile_fact_used=profile_fact_used,
            care_preference_version=(
                preference_version_value if preference_version_value > 0 else None
            ),
            allow_follow_up=bool(
                (care_preferences or {}).get("allow_follow_up", True)
            ),
        )

    def _risk_time(self, alert: Mapping[str, Any], local_date: date) -> datetime:
        parsed = _parse_datetime(
            alert.get("risk_time") or alert.get("time"),
            local_date=local_date,
            timezone_value=self.timezone,
        )
        return parsed or datetime.combine(local_date, time(12, 0), self.timezone)

    def _calendar_neighbours(
        self,
        events: list[Mapping[str, Any]],
        *,
        local_date: date,
        risk_time: datetime,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        normalized: list[tuple[datetime, datetime, dict[str, Any]]] = []
        for event in events[:100]:
            if not isinstance(event, Mapping):
                continue
            start = _parse_datetime(
                event.get("start_time"),
                local_date=local_date,
                timezone_value=self.timezone,
            )
            end = _parse_datetime(
                event.get("end_time"),
                local_date=local_date,
                timezone_value=self.timezone,
            )
            if start is None or end is None:
                continue
            if end < start:
                continue
            normalized.append((start, end, self._event_fact(event, start, end)))
        normalized.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
        active = [item for item in normalized if item[0] <= risk_time < item[1]]
        previous = [item for item in normalized if item[1] <= risk_time]
        following = [item for item in normalized if item[0] > risk_time]
        return (
            max(previous, key=lambda item: item[1])[2] if previous else None,
            min(active, key=lambda item: item[0])[2] if active else None,
            min(following, key=lambda item: item[0])[2] if following else None,
        )

    @staticmethod
    def _event_fact(
        event: Mapping[str, Any], start: datetime, end: datetime
    ) -> dict[str, Any]:
        summary = _text(event.get("summary") or event.get("name"), 120)
        course_name = _text(event.get("course_name"), 120)
        return {
            "id": _text(event.get("id") or event.get("event_id"), 160),
            "summary": summary,
            "display_name": course_name or summary or "一项安排",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "event_type": _text(event.get("event_type") or "other", 32),
            "task_type": _text(event.get("task_type") or "general", 32),
            "course_name": course_name or None,
            "course_code": _text(event.get("course_code"), 64) or None,
        }

    def _observation(
        self,
        value: Mapping[str, Any] | None,
        risk_time: datetime,
    ) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            return None
        observed_at = _parse_datetime(
            value.get("observed_at") or value.get("created_at"),
            local_date=risk_time.date(),
            timezone_value=self.timezone,
        )
        if observed_at is not None and observed_at > risk_time:
            return None
        stress = payload.get("stress_0_10")
        energy = payload.get("energy_0_10")
        if stress is None and energy is None:
            return None
        return {
            "id": _text(value.get("id") or value.get("observation_id"), 160),
            "observed_at": observed_at.isoformat() if observed_at else None,
            "stress_0_10": _zero_to_ten(stress) if stress is not None else None,
            "energy_0_10": _zero_to_ten(energy) if energy is not None else None,
            "activity": _text(payload.get("activity"), 80) or None,
        }

    @staticmethod
    def _profile_summary(
        profile: Mapping[str, Any] | None,
        *,
        profile_version: int | None,
        observation: Mapping[str, Any] | None,
        care_preferences: Mapping[str, Any] | None,
    ) -> CareProfileSummary:
        preferences: dict[str, str] = {}
        controlled_preference_used = False

        def visit(value: Mapping[str, Any], depth: int = 0) -> None:
            if depth > 2:
                return
            for key, raw in value.items():
                normalized = str(key).strip().casefold()
                destination = _PREFERENCE_KEYS.get(normalized)
                if destination and isinstance(raw, (str, int, float, bool)):
                    cleaned = _reviewed_preference(raw, destination)
                    if cleaned:
                        preferences.setdefault(destination, cleaned)
                elif normalized in {"care_preferences", "preferences", "care"} and isinstance(raw, Mapping):
                    visit(raw, depth + 1)

        if isinstance(care_preferences, Mapping):
            preferred_types = set(care_preferences.get("preferred_support_types") or [])
            if "walk" in preferred_types:
                preferences["recovery_preference"] = "短暂散步"
            elif "hydration" in preferred_types:
                preferences["recovery_preference"] = "补水"
            elif "recovery" in preferred_types:
                preferences["recovery_preference"] = "安静休息"
            if "trusted_person" in preferred_types:
                preferences["support_preference"] = "联系一位可信任的人"
            controlled_preference_used = bool(preferences)
        if not controlled_preference_used and isinstance(profile, Mapping):
            visit(profile)
        stress = observation.get("stress_0_10") if observation else None
        energy = observation.get("energy_0_10") if observation else None
        return CareProfileSummary(
            recent_stress_tendency=(
                "high" if stress is not None and float(stress) >= 7.0
                else "moderate" if stress is not None and float(stress) >= 4.0
                else "low" if stress is not None
                else None
            ),
            recent_energy_tendency=(
                "low" if energy is not None and float(energy) <= 3.5
                else "normal" if energy is not None
                else None
            ),
            recovery_preference=preferences.get("recovery_preference"),
            support_preference=preferences.get("support_preference"),
            care_preference=preferences.get("care_preference"),
            profile_version=(
                profile_version
                if preferences and not controlled_preference_used
                else None
            ),
        )

    @staticmethod
    def _bounded_list(value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(
            item
            for item in (_text(raw, 120) for raw in value[:8])
            if item
        )
