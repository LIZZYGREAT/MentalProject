"""Participant-bound morning-brief content preferences and topic subscriptions."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Database
from app.models import (
    ParticipantMorningBriefPreference,
    ParticipantMorningBriefTopic,
    utc_now,
)


def _aware(value: datetime | None) -> datetime:
    value = value or utc_now()
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class MorningBriefTopicRepository:
    def __init__(self, database: Database, *, max_topics: int = 20) -> None:
        self.database = database
        self.max_topics = max(1, min(int(max_topics), 20))

    @staticmethod
    def validate_topic_payload(
        *, topic_label: str, query_hints: list[str] | tuple[str, ...] = (),
        source_kinds: list[str] | tuple[str, ...] = ("web",), priority: int = 0,
    ) -> dict[str, Any]:
        label = " ".join(str(topic_label).split())
        if not label or len(label) > 160:
            raise ValueError("topic_label is empty or too long")
        hints = [" ".join(str(item).split()) for item in query_hints]
        if len(hints) > 8 or any(not item or len(item) > 240 for item in hints):
            raise ValueError("query_hints are outside the allowed bounds")
        kinds = list(dict.fromkeys(str(item).casefold() for item in source_kinds))
        if not kinds or not set(kinds).issubset({"web", "github", "paper", "api"}):
            raise ValueError("source_kinds are outside the allowed set")
        if isinstance(priority, bool) or not -10 <= int(priority) <= 10:
            raise ValueError("priority is outside the allowed range")
        return {"topic_label": label, "query_hints": hints, "source_kinds": kinds, "priority": int(priority)}

    def defaults(self) -> dict[str, Any]:
        return {
            "include_calendar": True, "include_reminders": True,
            "max_research_items": 10, "lookback_hours": 24,
            "language": "zh-CN", "revision": 0, "updated_at": None,
        }

    def get_preferences(self, participant_id: uuid.UUID) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(ParticipantMorningBriefPreference, participant_id)
            if row is None:
                return self.defaults()
            return self._preference_view(row)

    def update_preferences(self, participant_id: uuid.UUID, changes: Mapping[str, Any]) -> dict[str, Any]:
        with self.database.session() as session:
            result = self.update_preferences_in_session(session, participant_id, changes)
        return result

    def update_preferences_in_session(self, session: Session, participant_id: uuid.UUID, changes: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"include_calendar", "include_reminders", "max_research_items", "lookback_hours", "language"}
        if set(changes) - allowed:
            raise ValueError("unsupported morning brief preference")
        row = session.get(ParticipantMorningBriefPreference, participant_id, with_for_update=True)
        if row is None:
            row = ParticipantMorningBriefPreference(participant_id=participant_id)
            session.add(row)
            session.flush()
        for key, value in changes.items():
            if key in {"include_calendar", "include_reminders"}:
                value = bool(value)
            elif key == "max_research_items":
                if isinstance(value, bool) or not 1 <= int(value) <= 30:
                    raise ValueError("max_research_items is outside the allowed range")
                value = int(value)
            elif key == "lookback_hours":
                if isinstance(value, bool) or not 1 <= int(value) <= 24 * 30:
                    raise ValueError("lookback_hours is outside the allowed range")
                value = int(value)
            elif key == "language":
                value = " ".join(str(value).split())[:32]
                if not value:
                    raise ValueError("language is required")
            setattr(row, key, value)
        row.revision = int(row.revision or 0) + 1
        row.updated_at = utc_now()
        session.flush()
        return self._preference_view(row)

    def list_topics(self, participant_id: uuid.UUID, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        with self.database.session() as session:
            query = select(ParticipantMorningBriefTopic).where(
                ParticipantMorningBriefTopic.participant_id == participant_id
            )
            if enabled_only:
                query = query.where(ParticipantMorningBriefTopic.enabled.is_(True))
            rows = session.execute(query.order_by(
                ParticipantMorningBriefTopic.priority.desc(), ParticipantMorningBriefTopic.created_at
            )).scalars().all()
            return [self._topic_view(row) for row in rows]

    def add_topic(self, participant_id: uuid.UUID, *, topic_label: str, query_hints: list[str] | tuple[str, ...] = (), source_kinds: list[str] | tuple[str, ...] = ("web",), priority: int = 0) -> dict[str, Any]:
        payload = self.validate_topic_payload(topic_label=topic_label, query_hints=query_hints, source_kinds=source_kinds, priority=priority)
        with self.database.session() as session:
            return self._add_in_session(session, participant_id, payload)

    def remove_topic(self, participant_id: uuid.UUID, topic_label: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.execute(select(ParticipantMorningBriefTopic).where(
                ParticipantMorningBriefTopic.participant_id == participant_id,
                ParticipantMorningBriefTopic.topic_label == " ".join(str(topic_label).split()),
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return {"ok": False, "error": "morning_brief_topic_not_found"}
            row.enabled = False
            row.updated_at = utc_now()
            return {"ok": True, "topic": self._topic_view(row)}

    def apply_in_session(self, session: Session, participant_id: uuid.UUID, *, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if operation in {"add", "update"}:
            validated = self.validate_topic_payload(
                topic_label=str(payload.get("topic_label") or ""),
                query_hints=payload.get("query_hints") or (),
                source_kinds=payload.get("source_kinds") or ("web",),
                priority=int(payload.get("priority", 0)),
            )
            if operation == "add":
                return {"topic": self._add_in_session(session, participant_id, validated)}
            row = session.execute(select(ParticipantMorningBriefTopic).where(
                ParticipantMorningBriefTopic.participant_id == participant_id,
                ParticipantMorningBriefTopic.topic_label == validated["topic_label"],
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return {"topic": self._add_in_session(session, participant_id, validated)}
            row.query_hints_json = validated["query_hints"]
            row.source_kinds_json = validated["source_kinds"]
            row.priority = validated["priority"]
            row.enabled = True
            row.updated_at = utc_now()
            return {"topic": self._topic_view(row)}
        if operation == "remove":
            label = " ".join(str(payload.get("topic_label") or "").split())
            row = session.execute(select(ParticipantMorningBriefTopic).where(
                ParticipantMorningBriefTopic.participant_id == participant_id,
                ParticipantMorningBriefTopic.topic_label == label,
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return {"ok": False, "error": "morning_brief_topic_not_found"}
            row.enabled = False
            row.updated_at = utc_now()
            return {"topic": self._topic_view(row)}
        raise ValueError("unsupported morning brief topic operation")

    def _add_in_session(self, session: Session, participant_id: uuid.UUID, payload: Mapping[str, Any]) -> dict[str, Any]:
        count = session.execute(select(ParticipantMorningBriefTopic).where(
            ParticipantMorningBriefTopic.participant_id == participant_id,
            ParticipantMorningBriefTopic.enabled.is_(True),
        )).scalars().all()
        if len(count) >= self.max_topics:
            raise ValueError("morning_brief_topic_limit_reached")
        row = session.execute(select(ParticipantMorningBriefTopic).where(
            ParticipantMorningBriefTopic.participant_id == participant_id,
            ParticipantMorningBriefTopic.topic_label == payload["topic_label"],
        ).with_for_update()).scalar_one_or_none()
        if row is None:
            row = ParticipantMorningBriefTopic(
                participant_id=participant_id,
                topic_label=payload["topic_label"],
                query_hints_json=list(payload["query_hints"]),
                source_kinds_json=list(payload["source_kinds"]),
                priority=payload["priority"],
                enabled=True,
            )
            session.add(row)
        else:
            row.query_hints_json = list(payload["query_hints"])
            row.source_kinds_json = list(payload["source_kinds"])
            row.priority = payload["priority"]
            row.enabled = True
            row.updated_at = utc_now()
        session.flush()
        return self._topic_view(row)

    @staticmethod
    def _preference_view(row: ParticipantMorningBriefPreference) -> dict[str, Any]:
        return {"include_calendar": row.include_calendar, "include_reminders": row.include_reminders, "max_research_items": row.max_research_items, "lookback_hours": row.lookback_hours, "language": row.language, "revision": row.revision, "updated_at": row.updated_at.isoformat() if row.updated_at else None}

    @staticmethod
    def _topic_view(row: ParticipantMorningBriefTopic) -> dict[str, Any]:
        return {"id": str(row.id), "topic_label": row.topic_label, "query_hints": list(row.query_hints_json or []), "source_kinds": list(row.source_kinds_json or []), "priority": row.priority, "enabled": row.enabled, "created_at": _aware(row.created_at).isoformat(), "updated_at": _aware(row.updated_at).isoformat()}

