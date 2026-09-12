"""Application service for structured interaction preferences."""

from __future__ import annotations

from typing import Any

from app.services.preference_validator import normalize_rule, validate_style_changes


class InteractionPreferenceService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def get(self, participant_id):
        return self.repository.get(participant_id)

    def update_style(self, participant_id, changes: dict):
        return self.repository.update_style(participant_id, validate_style_changes(changes))

    def apply_rule(self, participant_id, raw_text: str):
        result = normalize_rule(raw_text)
        stored = []
        for accepted in result["accepted"]:
            stored.append(self.repository.add_rule(
                participant_id, safe_text=accepted["safe_text"],
                category=accepted["category"], value=accepted["value"],
            ))
            if accepted["category"] in {"verbosity", "tone", "suggestion_style"}:
                self.repository.update_style(
                    participant_id, {accepted["category"]: accepted["value"]}
                )
        return {"accepted": stored, "rejected": result["rejected"], "preferences": self.get(participant_id)}
