"""Build minimal, time-bounded psychological context from research state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _level(value: float) -> str:
    if value >= 7:
        return "high"
    if value >= 4:
        return "elevated"
    return "low"


_PSYCHOLOGICAL_RELEVANCE = re.compile(
    r"(?:情绪|心情|状态|难受|焦虑|紧张|烦躁|沮丧|低落|崩溃|孤独|压力|"
    r"疲劳|疲惫|累(?:了|坏|死|得)?|睡眠|失眠|没睡好|休息|恢复|"
    r"负荷|忙不过来|喘不过气|关心我|陪我|安慰我|听我说|"
    r"压力曲线|状态总结|总结.{0,6}状态|为什么.{0,8}提醒我)",
    re.I,
)


def psych_context_relevant(text: str, route_or_tool_intent: str | None = None) -> bool:
    """Use research state only when the current turn clearly needs it."""

    combined = " ".join((str(text), str(route_or_tool_intent or ""))).strip()
    return bool(combined and _PSYCHOLOGICAL_RELEVANCE.search(combined))


class PsychologicalContextBuilder:
    """Read existing longitudinal state; never create a psychological memory."""

    def __init__(self, *, observations: Any, slow_states: Any, appraisals: Any, learned_profiles: Any) -> None:
        self.observations = observations
        self.slow_states = slow_states
        self.appraisals = appraisals
        self.learned_profiles = learned_profiles

    def build(self, participant_id, *, current_text: str = "", now: datetime | None = None) -> dict[str, Any]:
        instant = now or datetime.now(timezone.utc)
        instant = instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant.astimezone(timezone.utc)
        profile = self.learned_profiles.runtime_active(participant_id)
        model_version = str((profile or {}).get("model_version") or "research-state-v1")
        features: list[dict[str, Any]] = []

        slow = [
            row for row in self.slow_states.history(participant_id, limit=3)
            if _aware(row["effective_at"]) + timedelta(days=7) > instant
        ]
        if slow:
            latest = slow[0]
            slow_source_at = _aware(latest["effective_at"])
            slow_valid_until = slow_source_at + timedelta(days=7)
            if latest.get("rolling_7d_stress") is not None:
                features.append(self._feature(
                    "recent_stress", _level(float(latest["rolling_7d_stress"])),
                    window="7d", source_at=slow_source_at,
                    valid_until=slow_valid_until, confidence=None,
                    source=latest.get("source") or "participant_slow_state",
                    evidence_type="derived_slow_state", model_version=model_version,
                ))
            if latest.get("rolling_7d_workload") is not None:
                features.append(self._feature(
                    "recent_workload", _level(float(latest["rolling_7d_workload"])),
                    window="7d", source_at=slow_source_at,
                    valid_until=slow_valid_until, confidence=None,
                    source=latest.get("source") or "participant_slow_state",
                    evidence_type="derived_slow_state", model_version=model_version,
                ))
            if len(slow) > 1 and latest.get("recent_recovery_quality") is not None and slow[1].get("recent_recovery_quality") is not None:
                delta = float(latest["recent_recovery_quality"]) - float(
                    slow[1]["recent_recovery_quality"]
                )
                direction = "improving" if delta > 0.5 else "declining" if delta < -0.5 else "stable"
                features.append(self._feature(
                    "recovery_trend", direction, window="7d",
                    source_at=slow_source_at, valid_until=slow_valid_until,
                    confidence=None,
                    source=latest.get("source") or "participant_slow_state",
                    evidence_type="derived_trend", model_version=model_version,
                ))

        recent_observations = [row for row in self.observations.recent(participant_id, limit=10) if _aware(row["observed_at"]) + timedelta(hours=24) > instant and "safety" not in str(row.get("type") or "").casefold() and "protected" not in str(row.get("type") or "").casefold()]
        if recent_observations and len(features) < 5:
            observation = recent_observations[0]
            observation_source_at = _aware(observation["observed_at"])
            payload = dict(observation.get("payload") or {})
            stress = payload.get("stress") if payload.get("stress") is not None else payload.get("stress_level")
            if isinstance(stress, (int, float)):
                features.append(self._feature(
                    "current_stress", _level(float(stress)), window="24h",
                    source_at=observation_source_at,
                    valid_until=observation_source_at + timedelta(hours=24),
                    confidence=1.0,
                    source="state_observation", evidence_type="self_report",
                    model_version="observation-v1",
                ))

        appraisals = [
            row
            for row in self.appraisals.history(participant_id, limit=5)
            if _aware(row["submitted_at"]) + timedelta(days=3) > instant
        ]
        for row in appraisals[:1]:
            if len(features) < 5 and row.get("frustration") is not None:
                appraisal_source_at = _aware(row["submitted_at"])
                features.append(self._feature(
                    "recent_frustration", _level(float(row["frustration"])),
                    window="3d", source_at=appraisal_source_at,
                    valid_until=appraisal_source_at + timedelta(days=3),
                    confidence=1.0, source="event_appraisal",
                    evidence_type="self_report", model_version=str(row.get("workload_model_version") or "appraisal-v1"),
                ))

        result = {"features": features[:5]}
        while len(json.dumps(result, ensure_ascii=False)) > 800 and result["features"]:
            result["features"].pop()
        return result

    @staticmethod
    def _feature(name: str, value: str, *, window: str, source_at: datetime, valid_until: datetime, confidence: float | None, source: str, evidence_type: str, model_version: str) -> dict[str, Any]:
        return {
            "feature": name, "value": value, "time_window": window,
            "source_at": source_at.astimezone(timezone.utc).isoformat(),
            "valid_until": valid_until.astimezone(timezone.utc).isoformat(),
            "confidence": (
                round(max(0.0, min(1.0, confidence)), 3)
                if confidence is not None else None
            ),
            "source": str(source)[:64], "evidence_type": evidence_type,
            "model_version": str(model_version)[:64],
        }
