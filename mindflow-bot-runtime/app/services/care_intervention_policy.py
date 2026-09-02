"""Deterministic intervention selection over factual care context."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Any

from app.services.care_context import CareContext


CARE_INTERVENTION_POLICY_VERSION = "care_intervention_policy.v3"
_DEADLINE = re.compile(
    r"ddl|deadline|截止|提交|交作业|报告|论文|答辩",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class CareMessagePlan:
    policy_version: str
    intervention_type: str
    template_id: str
    reason_code: str
    action_minutes: int
    care_action: str
    context_quality: str
    facts_used: tuple[str, ...]
    actions: tuple[str, ...]
    ranking_score: float
    preference_matched: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CareInterventionPolicy:
    def plan(self, context: CareContext) -> CareMessagePlan:
        level = self._level(context.warning_level)
        if level >= 3 or context.care_action == "pause_and_seek_support":
            return self._plan(
                context,
                intervention_type="pause_and_seek_support",
                template_id="pause-and-support-v1",
                reason_code="very_high_predicted_pressure",
                action_minutes=10,
            )

        if context.context_quality == "degraded":
            return self._plan(
                context,
                intervention_type="generic_fallback",
                template_id="generic-fallback-v1",
                reason_code="insufficient_context",
                action_minutes=5,
            )

        dense = self._dense_transition(context)
        deadline = self._has_deadline(context)
        has_workload = bool(
            context.current_events
            or context.dominant_stressors
            or context.previous_event
            or context.active_event
            or context.next_event
        )
        candidates: list[dict[str, Any]] = [
            {
                "intervention_type": "brief_check_in",
                "template_id": "brief-check-in-v1",
                "reason_code": "elevated_pressure_with_context",
                "action_minutes": 5,
                "score": 0.40,
            }
        ]
        if context.profile_summary.recent_energy_tendency == "low":
            candidates.append({
                "intervention_type": "recovery",
                "template_id": "recovery-v1",
                "reason_code": "low_recent_energy_before_risk",
                "action_minutes": 10,
                "score": 0.90,
            })
        if dense or has_workload:
            candidates.append({
                "intervention_type": "transition_buffer",
                "template_id": "transition-buffer-v1",
                "reason_code": (
                    "dense_schedule_before_high_risk"
                    if dense else "transition_support_preference"
                ),
                "action_minutes": 10,
                "score": 0.80 if dense else 0.31,
            })
        if deadline or has_workload:
            candidates.append({
                "intervention_type": "workload_decomposition",
                "template_id": "workload-decomposition-v1",
                "reason_code": (
                    "deadline_workload_near_risk"
                    if deadline else "decomposition_support_preference"
                ),
                "action_minutes": 15,
                "score": 0.78 if deadline else 0.32,
            })
        candidates.append({
            "intervention_type": "protected_break",
            "template_id": "protected-break-v1",
            "reason_code": (
                "sustained_high_pressure"
                if level >= 2 or context.care_action == "protected_break"
                else "protected_break_option"
            ),
            "action_minutes": 15,
            "score": (
                0.72
                if level >= 2 or context.care_action == "protected_break"
                else 0.33
            ),
        })
        candidates.append({
            "intervention_type": "micro_break",
            "template_id": "micro-break-v1",
            "reason_code": "short_transition_recovery",
            "action_minutes": 3,
            "score": 0.34,
        })
        if context.allow_schedule_suggestions and (dense or deadline):
            candidates.append({
                "intervention_type": "schedule_adjustment",
                "template_id": "schedule-adjustment-v1",
                "reason_code": "schedule_adjustment_allowed",
                "action_minutes": 10,
                "score": 0.86,
            })

        preference_boosts = {
            "micro_break": "micro_break",
            "task_decomposition": "workload_decomposition",
            "transition_buffer": "transition_buffer",
            "brief_check_in": "brief_check_in",
            "protected_break": "protected_break",
            "priority_review": "workload_decomposition",
            "hydration_movement": "recovery",
            "schedule_adjustment_suggestion": "schedule_adjustment",
        }
        preferred = set(context.profile_summary.preferred_support_types)
        for candidate in candidates:
            matched = next(
                (
                    preference
                    for preference, intervention_type in preference_boosts.items()
                    if preference in preferred
                    and candidate["intervention_type"] == intervention_type
                ),
                None,
            )
            candidate["preference_matched"] = matched
            candidate["score"] = min(
                1.0,
                float(candidate["score"]) + (0.12 if matched else 0.0),
            )
        selected = max(candidates, key=lambda candidate: float(candidate["score"]))
        return self._plan(
            context,
            intervention_type=str(selected["intervention_type"]),
            template_id=str(selected["template_id"]),
            reason_code=str(selected["reason_code"]),
            action_minutes=int(selected["action_minutes"]),
            ranking_score=float(selected["score"]),
            preference_matched=selected.get("preference_matched"),
        )

    @staticmethod
    def _plan(
        context: CareContext,
        *,
        intervention_type: str,
        template_id: str,
        reason_code: str,
        action_minutes: int,
        ranking_score: float = 1.0,
        preference_matched: str | None = None,
    ) -> CareMessagePlan:
        return CareMessagePlan(
            policy_version=CARE_INTERVENTION_POLICY_VERSION,
            intervention_type=intervention_type,
            template_id=template_id,
            reason_code=reason_code,
            action_minutes=action_minutes,
            care_action=context.care_action,
            context_quality=context.context_quality,
            facts_used=context.fact_codes,
            actions=(
                "helpful",
                "not_relevant",
                *(("snooze_30",) if context.allow_follow_up else ()),
                "disable_type",
            ),
            ranking_score=round(max(0.0, min(ranking_score, 1.0)), 3),
            preference_matched=preference_matched,
        )

    @staticmethod
    def _level(value: str) -> int:
        normalized = str(value).strip().casefold()
        if normalized in {"3", "red", "critical"}:
            return 3
        if normalized in {"2", "orange", "high"}:
            return 2
        return 1

    @staticmethod
    def _dense_transition(context: CareContext) -> bool:
        pairs = (
            (context.active_event, context.next_event),
            (context.previous_event, context.next_event),
        )
        for left, right in pairs:
            if not left or not right:
                continue
            try:
                gap = (
                    datetime.fromisoformat(str(right["start_time"]))
                    - datetime.fromisoformat(str(left["end_time"]))
                ).total_seconds() / 60.0
            except (KeyError, TypeError, ValueError):
                continue
            if gap <= 30.0:
                return True
        return len(context.current_events) >= 2

    @staticmethod
    def _has_deadline(context: CareContext) -> bool:
        events = (
            context.previous_event,
            context.active_event,
            context.next_event,
        )
        for event in events:
            if not event:
                continue
            if str(event.get("task_type") or "").casefold() == "ddl":
                return True
            if _DEADLINE.search(str(event.get("summary") or "")):
                return True
        return any(_DEADLINE.search(value) for value in context.dominant_stressors)
