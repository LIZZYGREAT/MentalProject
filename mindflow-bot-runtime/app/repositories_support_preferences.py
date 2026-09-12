"""Explicit support preferences, separate from memory and psychological state."""

from __future__ import annotations

import uuid

from app.db import Database
from app.models import ParticipantSupportPreference, utc_now


DEFAULT_SUPPORT_PREFERENCES = {
    "acknowledge_before_advice": True,
    "ask_before_suggestion": False,
    "max_suggestions": 3,
    "allow_supportive_follow_up": True,
    "preferred_support_style": "gentle",
}


class SupportPreferenceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, participant_id: uuid.UUID) -> dict:
        with self.database.session() as session:
            row = session.get(ParticipantSupportPreference, participant_id)
            if row is None:
                return dict(DEFAULT_SUPPORT_PREFERENCES)
            return {
                "acknowledge_before_advice": bool(row.acknowledge_before_advice),
                "ask_before_suggestion": bool(row.ask_before_suggestion),
                "max_suggestions": int(row.max_suggestions),
                "allow_supportive_follow_up": bool(row.allow_supportive_follow_up),
                "preferred_support_style": row.preferred_support_style,
            }

    def update(self, participant_id: uuid.UUID, changes: dict) -> dict:
        allowed = set(DEFAULT_SUPPORT_PREFERENCES)
        if not changes or set(changes) - allowed:
            raise ValueError("unsupported support preference field")
        if "max_suggestions" in changes and (
            not isinstance(changes["max_suggestions"], int)
            or isinstance(changes["max_suggestions"], bool)
            or not 1 <= changes["max_suggestions"] <= 5
        ):
            raise ValueError("max_suggestions must be between 1 and 5")
        if "preferred_support_style" in changes and changes["preferred_support_style"] not in {"gentle", "listening", "practical"}:
            raise ValueError("unsupported support style")
        for key in ("acknowledge_before_advice", "ask_before_suggestion", "allow_supportive_follow_up"):
            if key in changes and not isinstance(changes[key], bool):
                raise ValueError(f"{key} must be boolean")
        with self.database.session() as session:
            row = session.get(ParticipantSupportPreference, participant_id, with_for_update=True)
            if row is None:
                row = ParticipantSupportPreference(participant_id=participant_id, **DEFAULT_SUPPORT_PREFERENCES)
                session.add(row)
            for key, value in changes.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
        return self.get(participant_id)
