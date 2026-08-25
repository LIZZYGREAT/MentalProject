"""Tool-to-algorithm adapter with participant-scoped persistence."""

from __future__ import annotations

from typing import Any

from mindflow_core.assessment import AssessmentModel


class PredictionService:
    def __init__(self, model: AssessmentModel):
        self.model = model

    def calculate(
        self, *, profile: dict[str, Any], observations: list[dict[str, Any]],
        calendar_events: list[dict[str, Any]], calendar_degraded: bool,
        local_date: str,
        initial_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Calculate without adding message-level persistence side effects."""

        return self.model.predict(
            profile=profile,
            observations=observations,
            calendar_events=calendar_events,
            local_date=local_date,
            calendar_degraded=calendar_degraded,
            initial_state=initial_state,
        ).to_dict()
