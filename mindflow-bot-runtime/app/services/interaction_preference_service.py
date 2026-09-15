"""Application service for structured interaction preferences."""

from __future__ import annotations

from typing import Any

from app.repositories_support_preferences import validate_support_changes
from app.services.preference_validator import (
    validate_identity_changes,
    validate_style_changes,
)


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

    def update_support(self, participant_id, changes: dict):
        return self.update_preferences(
            participant_id, support_changes=changes
        )["support"]
