"""Structured interaction preferences, separate from participant memory."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db import Database
from app.models import (
    ParticipantInteractionRule,
    ParticipantInteractionStyle,
    ParticipantSupportPreference,
    utc_now,
)
from app.repositories_support_preferences import DEFAULT_SUPPORT_PREFERENCES


DEFAULT_STYLE = {
    "verbosity": "balanced", "tone": "warm",
    "suggestion_style": "light_suggestions",
}


class PreferenceRuleLimitReached(ValueError):
    code = "preference_rule_limit_reached"

    def __init__(self) -> None:
        super().__init__("你已经有 3 条自定义规则，可以先删除或替换一条。")


class InteractionPreferenceRepository:
    def __init__(self, database: Database, *, max_rules: int = 3) -> None:
        self.database = database
        self.max_rules = max(1, min(int(max_rules), 3))

    def get(self, participant_id: uuid.UUID) -> dict:
        with self.database.session() as session:
            style = session.get(ParticipantInteractionStyle, participant_id)
            rules = session.execute(select(ParticipantInteractionRule).where(
                ParticipantInteractionRule.participant_id == participant_id,
                ParticipantInteractionRule.status == "active",
            ).order_by(ParticipantInteractionRule.created_at)).scalars().all()
            return {
                **(DEFAULT_STYLE if style is None else {
                    "verbosity": style.verbosity, "tone": style.tone,
                    "suggestion_style": style.suggestion_style,
                }),
                "rules": [{
                    "id": str(row.id), "category": row.normalized_category,
                    "value": row.normalized_value,
                } for row in rules],
            }

    def update_style(self, participant_id: uuid.UUID, changes: dict) -> dict:
        with self.database.session() as session:
            row = session.get(ParticipantInteractionStyle, participant_id, with_for_update=True)
            if row is None:
                row = ParticipantInteractionStyle(participant_id=participant_id, **DEFAULT_STYLE)
                session.add(row)
            for key, value in changes.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
        return self.get(participant_id)

    def update_atomic(
        self,
        participant_id: uuid.UUID,
        *,
        style_changes: dict | None = None,
        support_changes: dict | None = None,
        identity_changes: dict[str, str] | None = None,
    ) -> None:
        """Commit all validated interaction preference changes together."""

        style_changes = dict(style_changes or {})
        support_changes = dict(support_changes or {})
        identity_changes = dict(identity_changes or {})
        if not (style_changes or support_changes or identity_changes):
            raise ValueError("at least one interaction preference is required")

        now = utc_now()
        with self.database.session() as session:
            active_rules = []
            if identity_changes:
                active_rules = session.execute(
                    select(ParticipantInteractionRule)
                    .where(
                        ParticipantInteractionRule.participant_id == participant_id,
                        ParticipantInteractionRule.status == "active",
                    )
                    .order_by(ParticipantInteractionRule.created_at)
                    .with_for_update()
                ).scalars().all()
                retained_rules = [
                    row
                    for row in active_rules
                    if row.normalized_category not in identity_changes
                ]
                if len(retained_rules) + len(identity_changes) > self.max_rules:
                    raise PreferenceRuleLimitReached()

            if style_changes:
                style = session.get(
                    ParticipantInteractionStyle,
                    participant_id,
                    with_for_update=True,
                )
                if style is None:
                    style = ParticipantInteractionStyle(
                        participant_id=participant_id, **DEFAULT_STYLE
                    )
                    session.add(style)
                for key, value in style_changes.items():
                    setattr(style, key, value)
                style.updated_at = now

            if support_changes:
                support = session.get(
                    ParticipantSupportPreference,
                    participant_id,
                    with_for_update=True,
                )
                if support is None:
                    support = ParticipantSupportPreference(
                        participant_id=participant_id,
                        **DEFAULT_SUPPORT_PREFERENCES,
                    )
                    session.add(support)
                for key, value in support_changes.items():
                    setattr(support, key, value)
                support.updated_at = now

            for old in active_rules:
                if old.normalized_category in identity_changes:
                    old.status = "superseded"
                    old.updated_at = now
            for category, value in identity_changes.items():
                label = (
                    "助手显示名"
                    if category == "assistant_display_name"
                    else "助手自称"
                )
                session.add(
                    ParticipantInteractionRule(
                        participant_id=participant_id,
                        raw_text=f"{label}：{value}"[:200],
                        normalized_category=category,
                        normalized_value=value,
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.flush()

    def add_rule(self, participant_id: uuid.UUID, *, safe_text: str, category: str, value: str) -> dict:
        stored_value = str(value).lower() if isinstance(value, bool) else str(value)
        now = utc_now()
        with self.database.session() as session:
            same = session.execute(select(ParticipantInteractionRule).where(
                ParticipantInteractionRule.participant_id == participant_id,
                ParticipantInteractionRule.status == "active",
                ParticipantInteractionRule.normalized_category == category,
            ).with_for_update()).scalars().all()
            for old in same:
                old.status = "superseded"
                old.updated_at = now
            active = session.execute(select(ParticipantInteractionRule).where(
                ParticipantInteractionRule.participant_id == participant_id,
                ParticipantInteractionRule.status == "active",
                ParticipantInteractionRule.normalized_category != category,
            ).order_by(ParticipantInteractionRule.created_at).with_for_update()).scalars().all()
            if len(active) >= self.max_rules:
                raise PreferenceRuleLimitReached()
            row = ParticipantInteractionRule(
                participant_id=participant_id, raw_text=safe_text[:200],
                normalized_category=category, normalized_value=stored_value,
                status="active", created_at=now, updated_at=now,
            )
            session.add(row)
            session.flush()
            return {"id": str(row.id), "category": category, "value": stored_value}
