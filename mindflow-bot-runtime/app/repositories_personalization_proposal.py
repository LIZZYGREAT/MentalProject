"""Participant-bound lifecycle for typed personalization proposals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Database
from app.models import Participant, PersonalizationProposal, utc_now


_OPERATIONS = {
    "memory": {"remember", "replace", "delete", "clear"},
    "interaction_preferences": {"update"},
    "support_preferences": {"update"},
    "care_preferences": {"update"},
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class PersonalizationProposalRepository:
    def __init__(self, database: Database, *, ttl_minutes: int = 30) -> None:
        self.database = database
        self.ttl = timedelta(minutes=max(1, int(ttl_minutes)))

    def stage(
        self,
        participant_id: uuid.UUID,
        *,
        domain: str,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if domain not in _OPERATIONS or operation not in _OPERATIONS[domain]:
            raise ValueError("unsupported personalization proposal")
        if not payload:
            raise ValueError("personalization proposal payload is required")
        now = utc_now()
        with self.database.session() as session:
            if session.get(Participant, participant_id) is None:
                raise ValueError("participant does not exist")
            row = PersonalizationProposal(
                participant_id=participant_id,
                domain=domain,
                operation=operation,
                payload_json=dict(payload),
                expires_at=now + self.ttl,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def get_for_participant(
        self, participant_id: uuid.UUID, proposal_id: uuid.UUID | str
    ) -> dict[str, Any] | None:
        try:
            parsed_id = uuid.UUID(str(proposal_id))
        except ValueError:
            return None
        with self.database.session() as session:
            row = session.execute(
                select(PersonalizationProposal).where(
                    PersonalizationProposal.id == parsed_id,
                    PersonalizationProposal.participant_id == participant_id,
                )
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def resolve_confirmed_atomically(
        self,
        participant_id: uuid.UUID,
        proposal_id: uuid.UUID | str,
        *,
        execute: Callable[[Session, dict[str, Any]], dict[str, Any]],
        before_terminal_status: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Apply one local effect and terminalize its proposal in one commit."""

        try:
            parsed_id = uuid.UUID(str(proposal_id))
        except ValueError:
            return {"ok": False, "error": "personalization_proposal_not_found"}
        now = utc_now()
        with self.database.session() as session:
            row = session.execute(
                select(PersonalizationProposal)
                .where(
                    PersonalizationProposal.id == parsed_id,
                    PersonalizationProposal.participant_id == participant_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return {"ok": False, "error": "personalization_proposal_not_found"}
            if row.status != "awaiting_confirmation":
                return {
                    "ok": False,
                    "error": "personalization_proposal_already_resolved",
                    "status": row.status,
                }
            if _aware(row.expires_at) <= now:
                row.status = "expired"
                row.updated_at = now
                row.resolved_at = now
                return {
                    "ok": False,
                    "error": "personalization_proposal_expired",
                    "status": "expired",
                }
            result = dict(execute(session, self._view(row)))
            if before_terminal_status is not None:
                before_terminal_status()
            row.status = "confirmed"
            row.result_json = result
            row.error_code = None
            row.updated_at = now
            row.resolved_at = now
            session.flush()
            return {"ok": True, "status": "confirmed", **result}

    def cancel(
        self,
        participant_id: uuid.UUID,
        proposal_id: uuid.UUID | str,
    ) -> dict[str, Any]:
        try:
            parsed_id = uuid.UUID(str(proposal_id))
        except ValueError:
            return {"ok": False, "error": "personalization_proposal_not_found"}
        now = utc_now()
        with self.database.session() as session:
            row = session.execute(
                select(PersonalizationProposal)
                .where(
                    PersonalizationProposal.id == parsed_id,
                    PersonalizationProposal.participant_id == participant_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return {"ok": False, "error": "personalization_proposal_not_found"}
            if row.status != "awaiting_confirmation":
                return {
                    "ok": False,
                    "error": "personalization_proposal_already_resolved",
                    "status": row.status,
                }
            if _aware(row.expires_at) <= now:
                row.status = "expired"
                row.updated_at = now
                row.resolved_at = now
                return {
                    "ok": False,
                    "error": "personalization_proposal_expired",
                    "status": "expired",
                }
            row.status = "cancelled"
            row.result_json = {"persisted": False}
            row.updated_at = now
            row.resolved_at = now
            return {
                "ok": True,
                "status": "cancelled",
                "persisted": False,
            }

    def fail_pending(
        self,
        participant_id: uuid.UUID,
        proposal_id: uuid.UUID | str,
        error_code: str,
    ) -> None:
        now = utc_now()
        with self.database.session() as session:
            row = session.execute(
                select(PersonalizationProposal)
                .where(
                    PersonalizationProposal.id == uuid.UUID(str(proposal_id)),
                    PersonalizationProposal.participant_id == participant_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is not None and row.status == "awaiting_confirmation":
                row.status = "failed"
                row.error_code = str(error_code)[:128]
                row.updated_at = now
                row.resolved_at = now

    @staticmethod
    def _view(row: PersonalizationProposal) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "domain": row.domain,
            "operation": row.operation,
            "payload": dict(row.payload_json or {}),
            "status": row.status,
            "result": dict(row.result_json or {}) if row.result_json else None,
            "error_code": row.error_code,
            "expires_at": _aware(row.expires_at).isoformat(),
            "created_at": _aware(row.created_at).isoformat(),
        }
