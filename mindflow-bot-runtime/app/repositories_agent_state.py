"""Durable participant-scoped backend facts for warm Agent continuity."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any, Mapping

from sqlalchemy import and_, or_, select

from app.db import Database
from app.models import (
    ClaudeSession,
    Participant,
    ParticipantAgentStateEvent,
    utc_now,
)


_EVENT_FIELDS = ("event_type", "resource_kind", "resource_id", "state", "summary")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ParticipantAgentStateEventRepository:
    """Append-only ledger; it never stores callback payloads or card JSON."""

    def __init__(self, database: Database, *, max_turn_updates: int = 20) -> None:
        self.database = database
        self.max_turn_updates = max(1, min(int(max_turn_updates), 50))

    def append(
        self,
        participant_id: uuid.UUID,
        update: Mapping[str, Any],
        *,
        occurred_at: datetime | None = None,
    ) -> dict[str, str | None]:
        event_type = str(update.get("event_type") or "").strip()
        resource_kind = str(update.get("resource_kind") or "").strip()
        resource_id = str(update.get("resource_id") or "").strip()
        state = str(update.get("state") or "").strip()
        if not event_type or not resource_kind or not resource_id or not state:
            raise ValueError("backend state event is incomplete")
        if len(event_type) > 64 or len(resource_kind) > 32 or len(resource_id) > 128:
            raise ValueError("backend state event field is too long")
        summary = str(update.get("summary") or "").strip()[:300] or None
        when = _aware(occurred_at or utc_now())
        with self.database.session() as session:
            if session.get(Participant, participant_id) is None:
                raise ValueError("participant does not exist")
            row = ParticipantAgentStateEvent(
                participant_id=participant_id,
                event_type=event_type,
                resource_kind=resource_kind,
                resource_id=resource_id,
                state=state,
                summary=summary,
                occurred_at=when,
                created_at=when,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def list_after(
        self,
        participant_id: uuid.UUID,
        after_event_id: uuid.UUID | str | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, str | None]]:
        """Return bounded events after the participant's opaque cursor."""

        bound = self.max_turn_updates if limit is None else max(1, min(int(limit), 50))
        with self.database.session() as session:
            query = select(ParticipantAgentStateEvent).where(
                ParticipantAgentStateEvent.participant_id == participant_id
            )
            if after_event_id:
                try:
                    cursor = uuid.UUID(str(after_event_id))
                except ValueError:
                    cursor = None
                if cursor is not None:
                    cursor_row = session.get(ParticipantAgentStateEvent, cursor)
                    if (
                        cursor_row is not None
                        and cursor_row.participant_id == participant_id
                    ):
                        query = query.where(
                            or_(
                                ParticipantAgentStateEvent.created_at
                                > cursor_row.created_at,
                                and_(
                                    ParticipantAgentStateEvent.created_at
                                    == cursor_row.created_at,
                                    ParticipantAgentStateEvent.id > cursor_row.id,
                                ),
                            )
                        )
            rows = session.execute(
                query.order_by(
                    ParticipantAgentStateEvent.created_at,
                    ParticipantAgentStateEvent.id,
                ).limit(bound)
            ).scalars().all()
            return [self._view(row) for row in rows]

    def pending_for_turn(
        self, participant_id: uuid.UUID, *, limit: int | None = None
    ) -> tuple[tuple[dict[str, str | None], ...], str | None]:
        with self.database.session() as session:
            saved = session.get(ClaudeSession, participant_id)
            cursor = saved.last_backend_state_event_id if saved else None
        rows = self.list_after(participant_id, cursor, limit=limit)
        latest = str(rows[-1]["id"]) if rows else None
        return tuple(rows), latest

    @staticmethod
    def _view(row: ParticipantAgentStateEvent) -> dict[str, str | None]:
        return {
            "id": str(row.id),
            "event_type": row.event_type,
            "resource_kind": row.resource_kind,
            "resource_id": row.resource_id,
            "state": row.state,
            "summary": row.summary,
            "occurred_at": _aware(row.occurred_at).isoformat(),
        }
