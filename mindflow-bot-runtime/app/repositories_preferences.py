"""Structured interaction preferences, separate from participant memory."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Database
from app.models import (
    ParticipantInteractionSemanticRule,
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
            semantic_rules = session.execute(
                select(ParticipantInteractionSemanticRule).where(
                    ParticipantInteractionSemanticRule.participant_id == participant_id,
                    ParticipantInteractionSemanticRule.status == "active",
                ).order_by(ParticipantInteractionSemanticRule.created_at)
            ).scalars().all()
            identity = {
                row.normalized_category: row.normalized_value
                for row in rules
            }
            return {
                **(DEFAULT_STYLE if style is None else {
                    "verbosity": style.verbosity, "tone": style.tone,
                    "suggestion_style": style.suggestion_style,
                }),
                "rules": [{
                    "id": str(row.id), "category": row.normalized_category,
                    "value": row.normalized_value,
                } for row in rules],
                "identity": identity,
                "semantic_rules": [{
                    "id": str(row.id),
                    "scope": row.scope,
                    "instruction": row.instruction,
                } for row in semantic_rules],
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
        custom_rules: list[dict[str, str]] | None = None,
    ) -> None:
        """Commit all validated interaction preference changes together."""

        style_changes = dict(style_changes or {})
        support_changes = dict(support_changes or {})
        identity_changes = dict(identity_changes or {})
        custom_rules = list(custom_rules or [])
        if not (style_changes or support_changes or identity_changes or custom_rules):
            raise ValueError("at least one interaction preference is required")

        with self.database.session() as session:
            self.update_atomic_in_session(
                session,
                participant_id,
                style_changes=style_changes,
                support_changes=support_changes,
                identity_changes=identity_changes,
                custom_rules=custom_rules,
            )

    def update_atomic_in_session(
        self,
        session: Session,
        participant_id: uuid.UUID,
        *,
        style_changes: dict | None = None,
        support_changes: dict | None = None,
        identity_changes: dict[str, str] | None = None,
        custom_rules: list[dict[str, str]] | None = None,
    ) -> None:
        style_changes = dict(style_changes or {})
        support_changes = dict(support_changes or {})
        identity_changes = dict(identity_changes or {})
        custom_rules = [dict(item) for item in (custom_rules or [])]
        if not (style_changes or support_changes or identity_changes or custom_rules):
            raise ValueError("at least one interaction preference is required")
        now = utc_now()
        semantic_active = []
        if custom_rules:
            semantic_active = session.execute(
                select(ParticipantInteractionSemanticRule)
                .where(
                    ParticipantInteractionSemanticRule.participant_id == participant_id,
                    ParticipantInteractionSemanticRule.status == "active",
                )
                .order_by(ParticipantInteractionSemanticRule.created_at)
                .with_for_update()
            ).scalars().all()
            new_scopes = {str(item["scope"]) for item in custom_rules}
            retained_semantic = [
                row for row in semantic_active if row.scope not in new_scopes
            ]
            if len(retained_semantic) + len(custom_rules) > self.max_rules:
                raise PreferenceRuleLimitReached()
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
        for old in semantic_active:
            if old.scope in {str(item["scope"]) for item in custom_rules}:
                old.status = "superseded"
                old.updated_at = now
        for item in custom_rules:
            session.add(
                ParticipantInteractionSemanticRule(
                    participant_id=participant_id,
                    scope=str(item["scope"]),
                    instruction=str(item["instruction"]),
                    status="active",
                    source_proposal_id=item.get("source_proposal_id"),
                    created_at=now,
                    updated_at=now,
                )
            )
        session.flush()

    def delete_semantic_rule_in_session(
        self, session: Session, participant_id: uuid.UUID, rule_id: uuid.UUID | str
    ) -> dict[str, str]:
        try:
            parsed_id = uuid.UUID(str(rule_id))
        except ValueError as exc:
            raise LookupError("semantic rule not found") from exc
        row = session.execute(
            select(ParticipantInteractionSemanticRule)
            .where(
                ParticipantInteractionSemanticRule.id == parsed_id,
                ParticipantInteractionSemanticRule.participant_id == participant_id,
                ParticipantInteractionSemanticRule.status == "active",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise LookupError("semantic rule not found")
        row.status = "deleted"
        row.updated_at = utc_now()
        session.flush()
        return {"operation": "delete_rule", "semantic_rule_id": str(row.id)}

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
