"""Shared contextual care planning and reviewed message rendering."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from app.services.care_context import CareContextBuilder
from app.services.care_evidence import CareEvidenceBuilder
from app.services.care_reason_policy import CARE_REASON_POLICY_VERSION
from app.contracts.care_evidence import CARE_EVIDENCE_SCHEMA_VERSION
from app.services.care_intervention_policy import (
    CARE_INTERVENTION_POLICY_VERSION,
    CareInterventionPolicy,
)
from app.services.care_templates import (
    CARE_TEMPLATE_LIBRARY_VERSION,
    CareTemplateLibrary,
)
from app.services.care_jitai import (
    CareJITAIEngine,
    normalized_intervention_types,
)


CARE_MESSAGE_SCHEMA_VERSION = "care_message.v4"


class CareMessageService:
    def __init__(self, timezone_name: str):
        self.contexts = CareContextBuilder(timezone_name)
        self.policy = CareInterventionPolicy()
        self.templates = CareTemplateLibrary()
        self.jitai = CareJITAIEngine(timezone_name)
        self.evidence = CareEvidenceBuilder(timezone_name)

    def contextualize_alert(
        self,
        alert: Mapping[str, Any],
        *,
        source: str,
        local_date: date,
        calendar_events: list[Mapping[str, Any]],
        calendar_degraded: bool,
        recent_observation: Mapping[str, Any] | None,
        profile: Mapping[str, Any] | None,
        profile_version: int | None,
        care_preferences: Mapping[str, Any] | None = None,
        care_history: Mapping[str, Any] | None = None,
        forecast_output: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        effective_preferences = dict(care_preferences or {})
        explicit_types = normalized_intervention_types(
            list(effective_preferences.get("preferred_support_types") or [])
        )
        effective_preferences["preferred_support_types"] = explicit_types
        if not explicit_types:
            effective_preferences["preferred_support_types"] = normalized_intervention_types(
                list(effective_preferences.get("inferred_support_types") or [])
            )
        effective_preferences["disabled_intervention_types"] = normalized_intervention_types(
            list(effective_preferences.get("disabled_intervention_types") or [])
        )
        context = self.contexts.build(
            source=source,
            local_date=local_date,
            alert=alert,
            calendar_events=calendar_events,
            calendar_degraded=calendar_degraded,
            recent_observation=recent_observation,
            profile=profile,
            profile_version=profile_version,
            care_preferences=effective_preferences or None,
        )
        base_evidence = self.evidence.build(
            source=source,
            local_date=local_date,
            alert=alert,
            forecast_output=forecast_output,
            calendar_events=calendar_events,
            recent_observation=recent_observation,
            profile=profile,
            care_preferences=effective_preferences or None,
            care_history=care_history,
        )
        plan = self.policy.plan(context, evidence=base_evidence)
        evidence_workload = (
            base_evidence.schedule.get("weighted_load")
            if base_evidence.event_facts
            else alert.get("workload", 0.0)
        )
        evidence_continuous = (
            base_evidence.trajectory.get("continuous_load_factor")
            if base_evidence.event_facts or (forecast_output or {}).get("trajectory")
            else alert.get("continuous_load_factor", 0.5)
        )
        jitai_alert = {
            **dict(alert),
            "workload": evidence_workload,
            "continuous_load_factor": evidence_continuous,
        }
        decision = self.jitai.decide(
            context=context,
            alert=jitai_alert,
            proposed_type=plan.intervention_type,
            preferences=effective_preferences,
            history=care_history,
        )
        if decision.option_type in set(
            effective_preferences.get("disabled_intervention_types") or []
        ):
            decision = type(decision)(
                **{
                    **decision.to_dict(),
                    "decision_rule": "hold_explicitly_disabled",
                    "scheduled_at": None,
                }
            )
        final_evidence = self.evidence.build(
            source=source,
            local_date=local_date,
            alert=alert,
            forecast_output=forecast_output,
            calendar_events=calendar_events,
            recent_observation=recent_observation,
            profile=profile,
            care_preferences=effective_preferences or None,
            care_history=care_history,
            intervention={
                **plan.to_dict(),
                "decision_rule": decision.decision_rule,
            },
        )
        rendered = self.templates.render(context, plan, evidence=final_evidence)
        plan_payload = {
            **plan.to_dict(),
            "option_type": decision.option_type,
            "vulnerability_score": decision.vulnerability_score,
            "receptivity_score": decision.receptivity_score,
            "decision_score": decision.decision_score,
            "decision_rule": decision.decision_rule,
            "scheduled_at": decision.scheduled_at,
            "jitai_decision": decision.to_dict(),
        }
        provenance = {
            "schema_version": CARE_MESSAGE_SCHEMA_VERSION,
            "source": context.source,
            "source_warning_id": None,
            "source_forecast_id": None,
            "forecast_version": None,
            "care_action": context.care_action,
            "intervention_type": plan.intervention_type,
            "care_policy_version": CARE_INTERVENTION_POLICY_VERSION,
            "template_id": rendered.template_id,
            "template_version": rendered.template_version,
            "template_library_version": CARE_TEMPLATE_LIBRARY_VERSION,
            "current_events": list(context.current_events),
            "dominant_stressors": list(context.dominant_stressors),
            "calendar_context_ids": list(context.calendar_context_ids),
            "observation_id": (
                context.recent_observation.get("id")
                if context.recent_observation
                else None
            ),
            "profile_version": (
                context.profile_summary.profile_version
                if context.profile_fact_used
                else None
            ),
            "care_preference_version": context.care_preference_version,
            "recent_observation_max_age_minutes": (
                context.recent_observation_max_age_minutes
            ),
            "context_quality": context.context_quality,
            "vulnerability_score": decision.vulnerability_score,
            "receptivity_score": decision.receptivity_score,
            "decision_score": decision.decision_score,
            "decision_rule": decision.decision_rule,
            "care_evidence_schema_version": CARE_EVIDENCE_SCHEMA_VERSION,
            "care_reason_policy_version": CARE_REASON_POLICY_VERSION,
            "care_evidence": final_evidence.to_dict(),
        }
        result = dict(alert)
        result.pop("message", None)
        result.update(
            {
                "message": rendered.message,
                "fallback_message": rendered.message,
                "care_plan": plan_payload,
                "care_evidence": final_evidence.to_dict(),
                "care_context": context.to_dict(),
                "care_provenance": provenance,
            }
        )
        return result
