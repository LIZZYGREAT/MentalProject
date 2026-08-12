"""Tool-to-algorithm adapter with participant-scoped persistence."""

from __future__ import annotations

import uuid
from typing import Any

from app.repositories import PredictionRepository
from mindflow_core.assessment import AssessmentModel


class PredictionService:
    def __init__(self, model: AssessmentModel, predictions: PredictionRepository):
        self.model = model
        self.predictions = predictions

    def calculate(
        self, *, profile: dict[str, Any], observations: list[dict[str, Any]],
        calendar_events: list[dict[str, Any]], calendar_degraded: bool,
        local_date: str,
    ) -> dict[str, Any]:
        """Calculate without adding message-level persistence side effects."""

        return self.model.predict(
            profile=profile,
            observations=observations,
            calendar_events=calendar_events,
            local_date=local_date,
            calendar_degraded=calendar_degraded,
        ).to_dict()

    def run(
        self,
        *,
        participant_id: uuid.UUID,
        profile_version: int | None,
        profile: dict[str, Any],
        observations: list[dict[str, Any]],
        calendar_events: list[dict[str, Any]],
        calendar_degraded: bool,
        local_date: str,
        source_message_id: str,
    ) -> dict[str, Any]:
        existing = self.predictions.by_source_message(
            participant_id, source_message_id
        )
        if existing is not None:
            return existing
        output = self.calculate(
            profile=profile, observations=observations,
            calendar_events=calendar_events, calendar_degraded=calendar_degraded,
            local_date=local_date,
        )
        prediction_id = self.predictions.save(
            participant_id,
            profile_version=profile_version,
            model_version=str(output["model_version"]),
            input_snapshot={
                "profile_version": profile_version,
                "profile": profile,
                "observations": observations,
                "calendar_events": calendar_events,
                "calendar_degraded": calendar_degraded,
                "local_date": local_date,
            },
            output=output,
            source_message_id=source_message_id,
        )
        return {"prediction_run_id": str(prediction_id), **output}
