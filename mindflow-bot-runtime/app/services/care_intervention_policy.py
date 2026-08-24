"""Deterministic intervention selection over factual care context."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Any

from app.services.care_context import CareContext


CARE_INTERVENTION_POLICY_VERSION = "care_intervention_policy.v1"
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CareInterventionPolicy:
    def plan(self, context: CareContext) -> CareMessagePlan:
        if context.context_quality == "degraded":
            return self._plan(
                context,
                intervention_type="generic_fallback",
                template_id="generic-fallback-v1",
                reason_code="insufficient_context",
                action_minutes=5,
            )

        level = self._level(context.warning_level)
        if level >= 3 or context.care_action == "pause_and_seek_support":
            return self._plan(
                context,
                intervention_type="pause_and_seek_support",
                template_id="pause-and-support-v1",
                reason_code="very_high_predicted_pressure",
                action_minutes=10,
            )

        if context.profile_summary.recent_energy_tendency == "low":
            return self._plan(
                context,
                intervention_type="recovery",
                template_id="recovery-v1",
                reason_code="low_recent_energy_before_risk",
                action_minutes=10,
            )

        if self._dense_transition(context):
            return self._plan(
                context,
                intervention_type="transition_buffer",
                template_id="transition-buffer-v1",
                reason_code="dense_schedule_before_high_risk",
                action_minutes=10,
            )

        if self._has_deadline(context):
            return self._plan(
                context,
                intervention_type="workload_decomposition",
                template_id="workload-decomposition-v1",
                reason_code="deadline_workload_near_risk",
                action_minutes=15,
            )

        if level >= 2 or context.care_action == "protected_break":
            return self._plan(
                context,
                intervention_type="protected_break",
                template_id="protected-break-v1",
                reason_code="sustained_high_pressure",
                action_minutes=15,
            )

        return self._plan(
            context,
            intervention_type="brief_check_in",
            template_id="brief-check-in-v1",
            reason_code="elevated_pressure_with_context",
            action_minutes=5,
        )

    @staticmethod
    def _plan(
        context: CareContext,
        *,
        intervention_type: str,
        template_id: str,
        reason_code: str,
        action_minutes: int,
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
                "mute_today",
                "helpful",
                "not_relevant",
            ),
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
