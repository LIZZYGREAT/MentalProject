"""Build a small, auditable care evidence packet from existing Forecast facts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app.contracts.care_evidence import CareEvidencePacket, CARE_EVIDENCE_SCHEMA_VERSION
from app.services.care_reason_policy import CareReasonSelector
from app.services.hierarchical_personalization import care_personalization_projection


_SEMANTIC_DIMENSIONS = ("difficulty", "cognitive_demand", "expected_effort", "time_pressure", "uncertainty")
_EVENT_TYPES = {"course", "exam", "task", "meeting", "library", "other"}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _bounded(value: Any, low: float = 0.0, high: float = 1.0, default: float | None = None) -> float | None:
    number = _number(value, default)
    return None if number is None else round(max(low, min(high, number)), 3)


def _text(value: Any, limit: int = 160) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _parse(value: Any, timezone: ZoneInfo, local_date: date) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = _text(value, 80)
        if not raw:
            return None
        if len(raw) in {5, 8} and raw[2:3] == ":":
            try:
                parsed = datetime.combine(local_date, datetime.strptime(raw[:5], "%H:%M").time())
            except ValueError:
                return None
        else:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _level(prior: Any) -> str:
    value = _bounded(prior, 0.0, 1.0, 0.0) or 0.0
    return "high" if value >= 0.66 else "medium" if value >= 0.40 else "low"


class CareEvidenceBuilder:
    CONTINUOUS_GAP_MINUTES = 20
    WINDOW_BEFORE_HOURS = 4
    WINDOW_AFTER_MINUTES = 90

    def __init__(self, timezone_name: str):
        self.timezone = ZoneInfo(timezone_name)
        self.reasons = CareReasonSelector()

    def build(
        self,
        *,
        source: str,
        local_date: date,
        alert: Mapping[str, Any],
        forecast_output: Mapping[str, Any] | None = None,
        calendar_events: list[Mapping[str, Any]] | None = None,
        recent_observation: Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | None = None,
        care_preferences: Mapping[str, Any] | None = None,
        care_history: Mapping[str, Any] | None = None,
        intervention: Mapping[str, Any] | None = None,
    ) -> CareEvidencePacket:
        output = dict(forecast_output or {})
        risk_time = self._risk_time(alert, local_date)
        event_facts = self._event_facts(
            list(calendar_events or output.get("classified_calendar_events") or []),
            local_date=local_date,
        )
        schedule = self._schedule(event_facts, risk_time)
        trajectory = self._trajectory(output, alert, risk_time, profile)
        recent_state = self._recent_state(recent_observation, risk_time)
        personalization = self._personalization(profile, care_preferences)
        history = self._history(care_history, care_preferences)
        public_event_facts = [
            {key: value for key, value in fact.items() if not key.startswith("_")}
            for fact in event_facts[:8]
        ]
        packet = CareEvidencePacket(
            schema_version=CARE_EVIDENCE_SCHEMA_VERSION,
            source=_text(source, 48) or "forecast_warning",
            local_date=local_date.isoformat(),
            risk={
                "risk_time": risk_time.isoformat(),
                "warning_level": _text(alert.get("tier") or alert.get("intensity_zone") or "1", 32),
                "predicted_stress_0_10": _bounded(alert.get("predicted_stress_0_10", alert.get("S", alert.get("stress_0_10"))), 0.0, 10.0),
                "predicted_vitality_0_10": _bounded(alert.get("predicted_vitality_0_10", alert.get("V", alert.get("vitality_0_10"))), 0.0, 10.0),
                "fatigue_0_1": _bounded(alert.get("fatigue_0_1", alert.get("F", alert.get("fatigue"))), 0.0, 1.0),
                "risk_duration_minutes": _number(alert.get("risk_duration_minutes", alert.get("duration_minutes")), 0.0),
                "trajectory": trajectory.get("trajectory"),
            },
            trajectory=trajectory,
            schedule=schedule,
            event_facts=tuple(public_event_facts),
            recent_state=recent_state,
            personalization=personalization,
            care_history=history,
            intervention={
                "allowed_type": _text((intervention or {}).get("intervention_type") or (intervention or {}).get("allowed_type"), 64),
                "action_minutes": int(_number((intervention or {}).get("action_minutes"), 0.0) or 0),
                "decision_rule": _text((intervention or {}).get("decision_rule") or "send_at_planned_time", 64),
            },
            reason_candidates=(),
        )
        return CareEvidencePacket(
            **{
                **packet.__dict__,
                "reason_candidates": self.reasons.select(packet),
            }
        )

    def _risk_time(self, alert: Mapping[str, Any], local_date: date) -> datetime:
        return _parse(alert.get("risk_time") or alert.get("time"), self.timezone, local_date) or datetime.combine(local_date, datetime.min.time(), self.timezone)

    def _event_facts(self, events: list[Mapping[str, Any]], *, local_date: date) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for index, event in enumerate(events[:32]):
            start = _parse(event.get("start_time"), self.timezone, local_date)
            end = _parse(event.get("end_time"), self.timezone, local_date)
            if start is None or end is None or end <= start:
                continue
            metadata = dict(event.get("metadata") or {})
            semantic = dict(metadata.get("semantic") or event.get("semantic") or {})
            values = dict(
                semantic.get("values")
                or (semantic.get("fused") or {}).get("objective_semantics")
                or semantic.get("objective_semantics")
                or event.get("semantic_values")
                or {}
            )
            selected = {key: _bounded(values.get(key), 0.0, 1.0) for key in _SEMANTIC_DIMENSIONS}
            selected = {key: value for key, value in selected.items() if value is not None}
            prior = _bounded(event.get("workload_prior", semantic.get("workload_prior")), 0.0, 1.0, 0.0) or 0.0
            classification = dict(metadata.get("classification") or {})
            catalog = self._course_catalog(event, classification)
            event_type = _text(event.get("event_type") or "other", 32).lower()
            if event_type not in _EVENT_TYPES:
                event_type = "other"
            fact_id = f"event:{_text(event.get('id') or event.get('event_id') or str(index), 120)}"
            duration = max(0.0, (end - start).total_seconds() / 60.0)
            facts.append({
                "fact_id": fact_id,
                "display_name": _text(event.get("display_name") or event.get("course_name") or event.get("summary") or event.get("name") or "一项安排", 80),
                "event_type": event_type,
                "task_type": _text(event.get("task_type") or "general", 32),
                "start_time": start.strftime("%H:%M"),
                "end_time": end.strftime("%H:%M"),
                "course_name": _text(event.get("course_name") or event.get("related_course_name"), 120) or None,
                "course_code": _text(event.get("course_code") or event.get("related_course_code"), 64) or None,
                "course_match_confidence": _bounded(event.get("course_match_confidence"), 0.0, 1.0),
                "duration_minutes": round(duration, 2),
                "workload_prior": prior,
                "workload_level": _level(prior),
                "semantic": selected,
                "semantic_source": _text(semantic.get("source") or event.get("semantic_source"), 32) or None,
                "semantic_confidence": _bounded(semantic.get("confidence", event.get("semantic_confidence")), 0.0, 1.0),
                "semantic_evidence_tags": self._tags(semantic) or [
                    _text(item, 48) for item in list(event.get("semantic_evidence_tags") or [])[:6] if _text(item, 48)
                ],
                "course_catalog": catalog,
                "_start": start,
                "_end": end,
            })
        return facts

    @staticmethod
    def _tags(semantic: Mapping[str, Any]) -> list[str]:
        external = dict(semantic.get("external") or {})
        return [_text(item, 48) for item in list(external.get("evidence_tags") or [])[:6] if _text(item, 48)]

    @staticmethod
    def _course_catalog(event: Mapping[str, Any], classification: Mapping[str, Any]) -> dict[str, Any] | None:
        course_match = dict(classification.get("course_match") or {})
        direct_confidence = _number(
            event.get("course_match_confidence", course_match.get("confidence")), 0.0
        ) or 0.0
        if (
            isinstance(event.get("course_catalog"), Mapping)
            and direct_confidence >= 0.55
        ):
            value = dict(event["course_catalog"])
            return {
                "canonical_name": _text(value.get("canonical_name"), 200),
                "code": _text(value.get("code"), 64),
                "credits": _number(value.get("credits")),
                "hours": _number(value.get("hours")),
                "hours_per_week": _number(value.get("hours_per_week")),
            }
        context = dict(classification.get("course_catalog_context") or {})
        candidates = [dict(item) for item in list(context.get("candidates") or []) if isinstance(item, Mapping)]
        name = _text(event.get("course_name") or event.get("related_course_name") or course_match.get("canonical_name"), 200)
        code = _text(event.get("course_code") or event.get("related_course_code") or course_match.get("code"), 64)
        confidence = _number(event.get("course_match_confidence", course_match.get("confidence")), 0.0) or 0.0
        matches = [
            item for item in candidates
            if (code and _text(item.get("code"), 64) == code)
            or (name and _text(item.get("canonical_name"), 200) == name)
        ]
        if len(matches) != 1 or confidence < 0.55:
            return None
        candidate = matches[0]
        return {
            "canonical_name": _text(candidate.get("canonical_name"), 200),
            "code": _text(candidate.get("code"), 64),
            "credits": _number(candidate.get("credits")),
            "hours": _number(candidate.get("hours")),
            "hours_per_week": _number(candidate.get("hours_per_week")),
        }

    def _schedule(self, facts: list[dict[str, Any]], risk_time: datetime) -> dict[str, Any]:
        visible = [
            fact for fact in facts
            if risk_time - timedelta(hours=self.WINDOW_BEFORE_HOURS) <= fact["_start"] <= risk_time + timedelta(minutes=self.WINDOW_AFTER_MINUTES)
        ]
        visible.sort(key=lambda item: item["_start"])
        courses = [fact for fact in visible if fact["event_type"] == "course"]
        longest_count = longest_minutes = largest_break = 0
        current_count = current_minutes = 0.0
        previous = None
        for fact in courses:
            gap = None if previous is None else max(0.0, (fact["_start"] - previous).total_seconds() / 60.0)
            if previous is None or (gap is not None and gap <= self.CONTINUOUS_GAP_MINUTES):
                current_count += 1
                current_minutes += fact["duration_minutes"]
            else:
                longest_count = max(longest_count, int(current_count))
                longest_minutes = max(longest_minutes, int(current_minutes))
                current_count, current_minutes = 1, fact["duration_minutes"]
            if gap is not None:
                largest_break = max(largest_break, int(gap))
            previous = fact["_end"]
        longest_count = max(longest_count, int(current_count))
        longest_minutes = max(longest_minutes, int(current_minutes))
        total_minutes = sum(float(item["duration_minutes"]) for item in visible)
        weighted = sum(float(item["duration_minutes"]) * float(item["workload_prior"]) for item in visible)
        return {
            "window_start": (risk_time - timedelta(hours=self.WINDOW_BEFORE_HOURS)).strftime("%H:%M"),
            "window_end": (risk_time + timedelta(minutes=self.WINDOW_AFTER_MINUTES)).strftime("%H:%M"),
            "continuous_course_count": longest_count,
            "consecutive_course_count": longest_count,
            "consecutive_course_minutes": longest_minutes,
            "largest_break_minutes": largest_break,
            "high_load_event_count": sum(item["workload_level"] == "high" for item in visible),
            "low_load_event_count": sum(item["workload_level"] == "low" for item in visible),
            "weighted_load": round(weighted / total_minutes, 3) if total_minutes else 0.0,
            "continuous_gap_policy_minutes": self.CONTINUOUS_GAP_MINUTES,
            "dominant_event_fact_ids": [
                item["fact_id"] for item in sorted(visible, key=lambda value: value["duration_minutes"] * value["workload_prior"], reverse=True)[:3]
            ],
        }

    def _trajectory(self, output: Mapping[str, Any], alert: Mapping[str, Any], risk_time: datetime, profile: Mapping[str, Any] | None) -> dict[str, Any]:
        points = [dict(item) for item in list(output.get("trajectory") or []) if isinstance(item, Mapping)]
        def stress(item: Mapping[str, Any]) -> float:
            return _number(item.get("stress_0_10", item.get("predicted_stress")), 0.0) or 0.0
        peak = max(points, key=stress, default={})
        initial = dict(output.get("initial_state") or {})
        initial_stress = _number(initial.get("stress_0_10"))
        baseline = self._baseline(profile)
        carryover = {
            "present": bool(initial.get("mode") in {"previous_day_forecast", "previous_day_daily_review"} and initial_stress is not None and initial_stress > baseline + 0.5),
            "source": initial.get("mode") if initial.get("mode") in {"previous_day_forecast", "previous_day_daily_review"} else None,
            "initial_stress_0_10": initial_stress,
            "initial_vitality_0_10": _number(initial.get("vitality_0_10")),
            "stress_above_baseline": round(max(0.0, (initial_stress or baseline) - baseline), 3),
        }
        factor = max((_bounded(item.get("continuous_load_factor", item.get("continuous_load")), 0.0, 1.0, 0.0) or 0.0) for item in points) if points else _bounded(alert.get("continuous_load_factor"), 0.0, 1.0, 0.0) or 0.0
        return {
            "peak_stress_0_10": round(stress(peak) if peak else (_number(alert.get("S"), 0.0) or 0.0), 3),
            "peak_time": _text(peak.get("time"), 16) or None,
            "continuous_load_factor": round(factor, 3),
            "continuous_load_penalty": round(max((_number(item.get("continuous_load_penalty"), 0.0) or 0.0) for item in points), 3) if points else 0.0,
            "recovery_window_before_risk": "insufficient" if factor >= 0.6 else "available",
            "previous_day_carryover": carryover,
            "trajectory": "rising" if len(points) >= 2 and stress(points[-1]) > stress(points[0]) + 0.3 else "plateau" if points else None,
        }

    @staticmethod
    def _baseline(profile: Mapping[str, Any] | None) -> float:
        params = dict((profile or {}).get("model_params") or (profile or {}).get("params") or {})
        return max(0.0, min(10.0, (_number(params.get("S_star_init"), 50.0) or 50.0) / 10.0))

    def _recent_state(self, observation: Mapping[str, Any] | None, risk_time: datetime) -> dict[str, Any]:
        value = dict(observation or {})
        payload = dict(value.get("payload") or value)
        observed_at = _parse(value.get("observed_at") or value.get("created_at"), self.timezone, risk_time.date())
        age = (risk_time - observed_at).total_seconds() / 60.0 if observed_at else None
        valid = age is not None and 0.0 <= age <= 360.0
        stress = _number(payload.get("stress_0_10"))
        energy = _number(payload.get("energy_0_10", payload.get("vitality_0_10")))
        return {
            "available": bool(valid and (stress is not None or energy is not None)),
            "stress_tendency": "high" if valid and stress is not None and stress >= 7 else "moderate" if valid and stress is not None and stress >= 4 else "low" if valid and stress is not None else None,
            "energy_tendency": "low" if valid and energy is not None and energy <= 3.5 else "normal" if valid and energy is not None else None,
            "source_age_minutes": round(max(0.0, age), 1) if valid and age is not None else None,
            "source_at": observed_at.isoformat() if valid and observed_at else None,
        }

    @staticmethod
    def _personalization(profile: Mapping[str, Any] | None, preferences: Mapping[str, Any] | None) -> dict[str, Any]:
        value = dict(profile or {})
        params = dict(value.get("model_params") or value.get("params") or {})
        provenance = dict(value.get("runtime_model_provenance") or {})
        selection = dict(params.get("model_selection") or value.get("model_selection") or {})
        promoted = provenance.get("provenance_type") == "stage5_promotion" or selection.get("status") == "stage5_promoted"
        if not promoted:
            return {
                "available": False,
                "source": "m0_or_unpromoted",
                "recovery_preference": CareEvidenceBuilder._preference(preferences),
                "support_preference": None,
            }
        projection = care_personalization_projection(params)
        projected_params = dict(projection.get("parameters") or {})
        population = dict(projection.get("population_prior") or {})
        def prior(name: str, fallback: float) -> float:
            raw = population.get(name, fallback)
            return _number(raw.get("mean") if isinstance(raw, Mapping) else raw, fallback) or fallback
        workload = _number(projected_params.get("workload_sensitivity_i"))
        recovery_rate = _number(projected_params.get("stress_recovery_rate_i"))
        reactivity = _number(projected_params.get("stress_reactivity_i"))
        return {
            "available": True,
            "source": "stage5_promoted",
            "workload_sensitivity": "above_population_prior" if workload is not None and workload > prior("workload_sensitivity_i", 2.8) else "near_population_prior",
            "recovery_rate": "slower_than_population_prior" if recovery_rate is not None and recovery_rate < prior("stress_recovery_rate_i", 0.5) else "near_population_prior",
            "stress_reactivity": "faster_than_population_prior" if reactivity is not None and reactivity > prior("stress_reactivity_i", 0.7) else "near_population_prior",
            "parameters": projected_params,
            "population_prior": population,
            "recovery_preference": CareEvidenceBuilder._preference(preferences),
            "support_preference": None,
        }

    @staticmethod
    def _preference(preferences: Mapping[str, Any] | None) -> str | None:
        types = set(str(item) for item in list((preferences or {}).get("preferred_support_types") or []))
        if "hydration_movement" in types or "walk" in types:
            return "短暂散步"
        if "micro_break" in types:
            return "短暂休息"
        if "recovery" in types:
            return "安静休息"
        return _text((preferences or {}).get("recovery_preference"), 80) or None

    @staticmethod
    def _history(history: Mapping[str, Any] | None, preferences: Mapping[str, Any] | None) -> dict[str, Any]:
        value = dict(history or {})
        explicit_summary = dict(value.get("explicit_helpful_summary") or {})
        preferred_from_feedback = [
            intervention_type
            for intervention_type, summary in explicit_summary.items()
            if isinstance(summary, Mapping)
            and int(summary.get("rated_count") or 0) >= 3
            and int(summary.get("helpful_count") or 0) >= 2
        ]
        return {
            "preferred_intervention_types": list(
                dict.fromkeys(
                    list(value.get("preferred_intervention_types") or [])
                    + preferred_from_feedback
                    + list((preferences or {}).get("preferred_support_types") or [])
                )
            )[:8],
            "disabled_intervention_types": list(value.get("disabled_intervention_types") or (preferences or {}).get("disabled_intervention_types") or [])[:8],
            "explicit_helpful_summary": explicit_summary,
            "causal_claim_allowed": False,
        }
