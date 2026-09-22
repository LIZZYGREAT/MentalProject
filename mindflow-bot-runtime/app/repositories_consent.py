"""Participant-owned consent records; external LLM never relies on legacy flags."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any, Optional

from sqlalchemy import select, text

from app.db import Database
from app.models import ParticipantConsent


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class ParticipantConsentRepository:
    """Versioned consent lifecycle storage; current record is the latest row.

    History-preserving rather than strictly append-only: revocation updates
    the current grant row in place (status/revoked_at) and prior rows remain
    queryable.
    """

    def __init__(self, database: Database):
        self.database = database

    def get_current(
        self, participant_id: uuid.UUID, consent_type: str
    ) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(ParticipantConsent)
                .where(
                    ParticipantConsent.participant_id == participant_id,
                    ParticipantConsent.consent_type == str(consent_type),
                )
                .order_by(
                    ParticipantConsent.consented_at.desc(),
                    ParticipantConsent.created_at.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._view(row)

    def grant(
        self,
        participant_id: uuid.UUID,
        consent_type: str,
        consent_version: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        consented_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            # Serialize check-then-insert per participant+type so two truly
            # concurrent accepts cannot both observe a stale inactive state
            # and insert duplicate active grants. PostgreSQL uses an advisory
            # transaction lock; SQLite serializes writes database-wide.
            if self.database.engine.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                    {
                        "lock_key": (
                            f"participant-consent:{participant_id}:{consent_type}"
                        )
                    },
                )
            current = session.execute(
                select(ParticipantConsent)
                .where(
                    ParticipantConsent.participant_id == participant_id,
                    ParticipantConsent.consent_type == str(consent_type),
                )
                .order_by(
                    ParticipantConsent.consented_at.desc(),
                    ParticipantConsent.created_at.desc(),
                )
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                current is not None
                and current.status == "active"
                and current.consent_version == str(consent_version)
            ):
                return self._view(current)
            row = ParticipantConsent(
                participant_id=participant_id,
                consent_type=str(consent_type),
                consent_version=str(consent_version),
                status="active",
                consented_at=consented_at,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def revoke(
        self,
        participant_id: uuid.UUID,
        consent_type: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        revoked_at = _aware(now or datetime.now(timezone.utc))
        with self.database.session() as session:
            rows = list(
                session.execute(
                    select(ParticipantConsent)
                    .where(
                        ParticipantConsent.participant_id == participant_id,
                        ParticipantConsent.consent_type == str(consent_type),
                        ParticipantConsent.status == "active",
                    )
                    .with_for_update()
                ).scalars()
            )
            for row in rows:
                row.status = "revoked"
                row.revoked_at = revoked_at
                row.updated_at = revoked_at
            if rows:
                session.flush()
            return bool(rows)

    def is_active(
        self,
        participant_id: uuid.UUID,
        consent_type: str,
        consent_version: str,
    ) -> bool:
        current = self.get_current(participant_id, consent_type)
        return bool(
            current
            and current["status"] == "active"
            and current["consent_version"] == str(consent_version)
        )

    @staticmethod
    def _view(row: ParticipantConsent) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "consent_type": row.consent_type,
            "consent_version": row.consent_version,
            "status": row.status,
            "consented_at": row.consented_at,
            "revoked_at": row.revoked_at,
        }
