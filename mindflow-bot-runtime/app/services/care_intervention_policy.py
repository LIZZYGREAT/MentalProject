"""Deterministic intervention selection over factual care context."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Any

from app.services.care_context import CareContext
from app.services.care_jitai import normalized_intervention_type


CARE_INTERVENTION_POLICY_VERSION = "care_intervention_policy.v4"
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
    def plan(self, context: CareContext, *, evidence: Any | None = None) -> CareMessagePlan:
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
        reason_codes = {
            str(item.get("code") or "")
            for item in list(getattr(evidence, "reason_candidates", ()) or ())
        }
        evidence_schedule = dict(getattr(evidence, "schedule", {}) or {})
        evidence_trajectory = dict(getattr(evidence, "trajectory", {}) or {})
        evidence_personalization = dict(getattr(evidence, "personalization", {}) or {})
        longitudinal = dict(getattr(evidence, "longitudinal_state", {}) or {})
        has_evidence = evidence is not None
        evidence_has_qualified_events = any(
            float(fact.get("workload_prior") or 0.0) > 0.0
            or bool(fact.get("semantic"))
            for fact in list(getattr(evidence, "event_facts", ()) or ())
        ) if has_evidence else False
        evidence_has_course_block = any(
            int(block.get("course_count") or 0) >= 2
            for block in list(evidence_schedule.get("continuous_course_blocks") or [])
        ) if has_evidence else False
        evidence_dense = "dense_high_load_course_block" in reason_codes
        evidence_recovery_window = (
            "insufficient_recovery_window" in reason_codes
            and float(evidence_schedule.get("weighted_load") or 0.0) >= 0.60
        )
        dense_signal = (
            evidence_dense or evidence_recovery_window
            if has_evidence and evidence_has_qualified_events and evidence_has_course_block
            else dense
        )
        sustained_load = (
            "sustained_continuous_load" in reason_codes
            or float(evidence_trajectory.get("local_continuous_load_factor") or 0.0) >= 0.60
        ) if has_evidence else False
        carryover = "previous_day_carryover" in reason_codes
        slow_recovery = evidence_personalization.get("recovery_rate") == "slower_than_population_prior"
        declining_recovery = longitudinal.get("recovery_trend") == "declining"
        low_energy = "low_recent_energy" in reason_codes
        high_task = self._has_high_workload_task(evidence) if has_evidence else False
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
        if context.profile_summary.recent_energy_tendency == "low" or low_energy or carryover or slow_recovery or declining_recovery:
            candidates.append({
                "intervention_type": "recovery",
                "template_id": "recovery-v1",
                "reason_code": (
                    "low_recent_energy_before_risk"
                    if low_energy or context.profile_summary.recent_energy_tendency == "low"
                    else "declining_recovery_trend"
                    if declining_recovery
                    else "personal_recovery_evidence"
                ),
                "action_minutes": 10,
                "score": 0.94 if carryover or slow_recovery or declining_recovery else 0.90,
            })
        if dense_signal or has_workload:
            candidates.append({
                "intervention_type": "transition_buffer",
                "template_id": "transition-buffer-v1",
                "reason_code": (
                    "dense_high_load_course_block"
                    if evidence_dense
                    else "insufficient_recovery_window"
                    if evidence_recovery_window
                    else "dense_schedule_before_high_risk"
                    if dense else "transition_support_preference"
                ),
                "action_minutes": 10,
                "score": (
                    0.88
                    if evidence_dense or evidence_recovery_window
                    else 0.80
                    if dense_signal
                    else 0.31
                ),
            })
        if deadline or high_task or has_workload:
            candidates.append({
                "intervention_type": "workload_decomposition",
                "template_id": "workload-decomposition-v1",
                "reason_code": (
                    "deadline_workload_near_risk"
                    if deadline
                    else "single_high_load_event"
                    if high_task
                    else "decomposition_support_preference"
                ),
                "action_minutes": 15,
                "score": 0.82 if deadline or high_task else 0.32,
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
                0.84
                if sustained_load
                else 0.72
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
        if context.allow_schedule_suggestions and (dense_signal or deadline):
            candidates.append({
                "intervention_type": "schedule_adjustment",
                "template_id": "schedule-adjustment-v1",
                "reason_code": "schedule_adjustment_allowed",
                "action_minutes": 10,
                "score": 0.86,
            })

        preferred = set(context.profile_summary.preferred_support_types)
        for candidate in candidates:
            normalized_type = normalized_intervention_type(
                candidate["intervention_type"]
            )
            matched = normalized_type if normalized_type in preferred else None
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
    def _has_high_workload_task(evidence: Any) -> bool:
        if evidence is None:
            return False
        reason_ids = {
            str(fact_id)
            for reason in list(getattr(evidence, "reason_candidates", ()) or ())
            if str(reason.get("code") or "") == "single_high_load_event"
            for fact_id in list(reason.get("fact_ids") or [])
        }
        if not reason_ids:
            return False
        return any(
            str(fact.get("fact_id") or "") in reason_ids
            and str(fact.get("event_type") or "") in {"task", "exam"}
            for fact in list(getattr(evidence, "event_facts", ()) or ())
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
                "ack",
                *(("snooze_30",) if context.allow_follow_up else ()),
                "helpful",
                "not_relevant",
                "mute_today",
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
