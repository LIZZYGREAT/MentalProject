"""Durable CardAction receipt and replay barrier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, select
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
    def __init__(self, database: Database, *, ttl_hours: int = 168):
        if not 24 <= int(ttl_hours) <= 720:
            raise ValueError("CardAction receipt TTL must be between 24 and 720 hours")
        self.database = database
        self.ttl_hours = int(ttl_hours)

    def claim(
        self,
        *,
        event_id: str,
        participant_id: uuid.UUID,
        action_name: str,
        action_version: str,
        action_fingerprint: str,
        message_id_hash: str,
        now: datetime | None = None,
    ) -> CardActionClaim:
        created_at = now or datetime.now(timezone.utc)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("CardAction receipt timestamp must include a timezone")
        created_at = created_at.astimezone(timezone.utc)
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
                        created_at=created_at,
                        expires_at=created_at + timedelta(hours=self.ttl_hours),
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

    def purge_expired(
        self,
        *,
        now: datetime | None = None,
        limit: int = 500,
    ) -> int:
        if not 1 <= int(limit) <= 5000:
            raise ValueError(
                "CardAction receipt purge limit must be between 1 and 5000"
            )
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("CardAction receipt purge timestamp must include a timezone")
        instant = instant.astimezone(timezone.utc)
        with self.database.session() as session:
            event_ids = list(
                session.scalars(
                    select(CardActionReceipt.event_id)
                    .where(CardActionReceipt.expires_at <= instant)
                    .order_by(CardActionReceipt.expires_at, CardActionReceipt.event_id)
                    .limit(int(limit))
                    .with_for_update(skip_locked=True)
                )
            )
            if not event_ids:
                return 0
            session.execute(
                delete(CardActionReceipt).where(
                    CardActionReceipt.event_id.in_(event_ids)
                )
            )
            return len(event_ids)
