"""Shared contextual care planning and reviewed message rendering."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from app.services.care_context import CareContextBuilder
from app.services.care_intervention_policy import (
    CARE_INTERVENTION_POLICY_VERSION,
    CareInterventionPolicy,
)
from app.services.care_templates import (
    CARE_TEMPLATE_LIBRARY_VERSION,
    CareTemplateLibrary,
)


CARE_MESSAGE_SCHEMA_VERSION = "care_message.v2"


class CareMessageService:
    def __init__(self, timezone_name: str):
        self.contexts = CareContextBuilder(timezone_name)
        self.policy = CareInterventionPolicy()
        self.templates = CareTemplateLibrary()

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
    ) -> dict[str, Any]:
        context = self.contexts.build(
            source=source,
            local_date=local_date,
            alert=alert,
            calendar_events=calendar_events,
            calendar_degraded=calendar_degraded,
            recent_observation=recent_observation,
            profile=profile,
            profile_version=profile_version,
            care_preferences=care_preferences,
        )
        plan = self.policy.plan(context)
        rendered = self.templates.render(context, plan)
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
        }
        result = dict(alert)
        result.pop("message", None)
        result.update(
            {
                "message": rendered.message,
                "care_plan": plan.to_dict(),
                "care_context": context.to_dict(),
                "care_provenance": provenance,
            }
        )
        return result
