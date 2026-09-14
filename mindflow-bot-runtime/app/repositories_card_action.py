"""Durable CardAction receipt and replay barrier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.models import CardActionReceipt


@dataclass(frozen=True)
class CardActionClaim:
    outcome: str
    status: str
    result_kind: str | None = None
    error_code: str | None = None


class CardActionReceiptRepository:
    def __init__(self, database: Database):
        self.database = database

    def claim(
        self,
        *,
        event_id: str,
        participant_id: uuid.UUID,
        action_name: str,
        action_version: str,
        action_fingerprint: str,
        message_id_hash: str,
    ) -> CardActionClaim:
        try:
            with self.database.session() as session:
                session.add(
                    CardActionReceipt(
                        event_id=event_id,
                        participant_id=participant_id,
                        action_name=action_name,
                        action_version=action_version,
                        action_fingerprint=action_fingerprint,
                        message_id_hash=message_id_hash,
                        status="processing",
                    )
                )
                session.flush()
            return CardActionClaim(outcome="claimed", status="processing")
        except IntegrityError:
            with self.database.session() as session:
                row = session.get(CardActionReceipt, event_id)
                if row is None:
                    raise
                if (
                    row.participant_id != participant_id
                    or row.action_name != action_name
                    or row.action_version != action_version
                    or row.action_fingerprint != action_fingerprint
                    or row.message_id_hash != message_id_hash
                ):
                    return CardActionClaim(outcome="conflict", status=row.status)
                return CardActionClaim(
                    outcome="replay",
                    status=row.status,
                    result_kind=row.result_kind,
                    error_code=row.error_code,
                )

    def complete(
        self,
        event_id: str,
        *,
        action_fingerprint: str,
        status: str,
        result_kind: str,
        error_code: str | None = None,
    ) -> None:
        if status not in {"succeeded", "rejected", "failed"}:
            raise ValueError("invalid CardAction receipt status")
        if result_kind not in {"navigation", "mutation", "error"}:
            raise ValueError("invalid CardAction receipt result kind")
        with self.database.session() as session:
            row = session.get(CardActionReceipt, event_id, with_for_update=True)
            if row is None or row.action_fingerprint != action_fingerprint:
                raise RuntimeError("CardAction receipt ownership mismatch")
            if row.status != "processing":
                return
            row.status = status
            row.result_kind = result_kind
            row.error_code = str(error_code)[:64] if error_code else None
            row.completed_at = datetime.now(timezone.utc)
