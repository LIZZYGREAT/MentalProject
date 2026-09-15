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

    @staticmethod
    def validate_changes(
        *,
        style_changes: dict | None = None,
        support_changes: dict | None = None,
        identity_changes: dict | None = None,
    ) -> dict[str, dict]:
        return {
            "style_changes": (
                validate_style_changes(style_changes) if style_changes else {}
            ),
            "support_changes": (
                validate_support_changes(support_changes) if support_changes else {}
            ),
            "identity_changes": validate_identity_changes(identity_changes or {}),
        }

    def update_preferences(
        self,
        participant_id,
        *,
        style_changes: dict | None = None,
        support_changes: dict | None = None,
        identity_changes: dict | None = None,
    ):
        validated = self.validate_changes(
            style_changes=style_changes,
            support_changes=support_changes,
            identity_changes=identity_changes,
        )
        self.repository.update_atomic(
            participant_id,
            style_changes=validated["style_changes"],
            support_changes=validated["support_changes"],
            identity_changes=validated["identity_changes"],
        )
        return self.get(participant_id)

    def update_support(self, participant_id, changes: dict):
        return self.update_preferences(
            participant_id, support_changes=changes
        )["support"]
