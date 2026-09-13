"""Application service for structured interaction preferences."""

from __future__ import annotations

from typing import Any

from app.services.preference_validator import normalize_rule, validate_style_changes


STYLE_FIELDS = frozenset({"verbosity", "tone", "suggestion_style"})
SUPPORT_FIELDS = frozenset({
    "acknowledge_before_advice", "ask_before_suggestion", "max_suggestions",
    "allow_supportive_follow_up", "preferred_support_style",
})


class InteractionPreferenceService:
    def __init__(self, repository: Any, support_repository: Any) -> None:
        self.repository = repository
        self.support_repository = support_repository

    def get(self, participant_id):
        return {
            **self.repository.get(participant_id),
            "support": self.support_repository.get(participant_id),
        }

    def update_style(self, participant_id, changes: dict):
        return self.repository.update_style(participant_id, validate_style_changes(changes))

    def apply_rule(self, participant_id, raw_text: str):
        result = normalize_rule(raw_text)
        stored = []
        for accepted in result["accepted"]:
            if accepted["category"] in STYLE_FIELDS:
                self.repository.update_style(
                    participant_id, {accepted["category"]: accepted["value"]}
                )
                stored.append(dict(accepted))
            elif accepted["category"] in SUPPORT_FIELDS:
                self.support_repository.update(
                    participant_id, {accepted["category"]: accepted["value"]}
                )
                stored.append(dict(accepted))
            else:
                stored.append(self.repository.add_rule(
                    participant_id, safe_text=accepted["safe_text"],
                    category=accepted["category"], value=accepted["value"],
                ))
        return {"accepted": stored, "rejected": result["rejected"], "preferences": self.get(participant_id)}

    def update_support(self, participant_id, changes: dict):
        return self.support_repository.update(participant_id, changes)
