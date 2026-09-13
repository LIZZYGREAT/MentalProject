"""Application service for structured interaction preferences."""

from __future__ import annotations

from typing import Any

from app.repositories_preferences import PreferenceRuleLimitReached
from app.repositories_support_preferences import validate_support_changes
from app.services.preference_validator import (
    normalize_rule,
    validate_identity_changes,
    validate_style_changes,
)


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
        return self.update_preferences(
            participant_id, style_changes=changes
        )

    def update_preferences(
        self,
        participant_id,
        *,
        style_changes: dict | None = None,
        support_changes: dict | None = None,
        identity_changes: dict | None = None,
    ):
        validated_style = (
            validate_style_changes(style_changes) if style_changes else {}
        )
        validated_support = (
            validate_support_changes(support_changes) if support_changes else {}
        )
        validated_identity = validate_identity_changes(identity_changes or {})
        self.repository.update_atomic(
            participant_id,
            style_changes=validated_style,
            support_changes=validated_support,
            identity_changes=validated_identity,
        )
        return self.get(participant_id)

    def apply_rule(self, participant_id, raw_text: str):
        result = normalize_rule(raw_text)
        stored = []
        if not result["accepted"]:
            return {
                "accepted": [],
                "rejected": result["rejected"],
                "error": "unsupported_interaction_rule",
                "reason_code": "rule_not_normalized",
                "preferences": self.get(participant_id),
            }
        style_changes = {}
        support_changes = {}
        identity_changes = {}
        for accepted in result["accepted"]:
            category = accepted["category"]
            if category in STYLE_FIELDS:
                style_changes[category] = accepted["value"]
            elif category in SUPPORT_FIELDS:
                support_changes[category] = accepted["value"]
            else:
                identity_changes[category] = accepted["value"]
            stored.append(dict(accepted))
        try:
            preferences = self.update_preferences(
                participant_id,
                style_changes=style_changes,
                support_changes=support_changes,
                identity_changes=identity_changes,
            )
        except PreferenceRuleLimitReached as exc:
            return {
                "accepted": [], "rejected": result["rejected"],
                "error": exc.code, "reason_code": exc.code,
                "message": str(exc),
                "preferences": self.get(participant_id),
            }
        return {
            "accepted": stored, "rejected": result["rejected"], "error": None,
            "reason_code": None,
            "preferences": preferences,
        }

    def update_support(self, participant_id, changes: dict):
        return self.update_preferences(
            participant_id, support_changes=changes
        )["support"]
