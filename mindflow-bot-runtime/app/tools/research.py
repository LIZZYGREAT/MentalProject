"""Scoped research aggregate tools; never return participant-level data."""

from __future__ import annotations

from typing import Any


RESEARCH_SCOPE = "research_aggregate_read"


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "date_start": {"type": "string", "format": "date"},
            "date_end": {"type": "string", "format": "date"},
        },
        "required": ["date_start", "date_end"],
    }


class ResearchTools:
    def __init__(self, service: Any) -> None:
        self.service = service

    def register(self, registry: Any) -> None:
        tools = {
            "research_get_weekly_stress_summary": self.service.weekly_stress_summary,
            "research_get_checkin_completion_summary": self.service.checkin_completion_summary,
            "research_get_intervention_response_summary": self.service.intervention_response_summary,
            "research_get_longitudinal_state_distribution": self.service.longitudinal_state_distribution,
        }
        for name, operation in tools.items():
            def handler(ctx, arguments, *, execute=operation):
                return execute(arguments["date_start"], arguments["date_end"])

            registry.register(
                name,
                "Return a de-identified read-only cohort aggregate. Small cohorts are suppressed.",
                _schema(),
                handler,
                effect="read",
                authorization_requirement="none",
                required_scope=RESEARCH_SCOPE,
            )
