"""Best-effort post-commit materialization of Stage-6 care outcomes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select

from app.db import Database
from app.models import (
    CareInterventionEvent,
    CareInterventionOutcome,
    StateObservation,
)
from app.services.care_effectiveness import CareEffectivenessService


logger = logging.getLogger(__name__)


class CareOutcomeRefreshService:
    """Refresh one committed check-in without coupling it to the core write."""

    def __init__(self, database: Database):
        self.database = database

    def on_observation_committed(
        self,
        participant_id: uuid.UUID,
        observation_id: uuid.UUID,
    ) -> bool:
        try:
            with self.database.session() as session:
                observation = session.get(StateObservation, observation_id)
                if (
                    observation is None
                    or observation.participant_id != participant_id
                    or observation.observation_type != "checkin"
                ):
                    return False
                CareEffectivenessService.attach_observation_in_session(
                    session, observation
                )
            return True
        except Exception:
            logger.exception(
                "care outcome refresh failed after observation commit",
                extra={
                    "participant_id": str(participant_id),
                    "observation_id": str(observation_id),
                },
            )
            return False

    def reconcile_recent(
        self,
        participant_id: uuid.UUID,
        *,
        as_of: datetime,
        lookback_hours: int = 24,
    ) -> dict[str, int]:
        """Idempotently repair recent outcomes using a bounded source window."""

        if as_of.tzinfo is None:
            raise ValueError("care outcome reconcile as_of must be timezone-aware")
        if not 1 <= int(lookback_hours) <= 24:
            raise ValueError("care outcome reconcile lookback_hours must be 1-24")
        cutoff = as_of.astimezone(timezone.utc)
        window_start = cutoff - timedelta(hours=int(lookback_hours))
        scanned = created = updated = 0
        with self.database.session() as session:
            interventions = session.execute(
                select(CareInterventionEvent).where(
                    CareInterventionEvent.participant_id == participant_id,
                    CareInterventionEvent.sent_at.is_not(None),
                    CareInterventionEvent.sent_at >= window_start,
                    CareInterventionEvent.sent_at <= cutoff,
                )
            ).scalars().all()
            from app.repositories_care import CareInterventionRepository

            for intervention in interventions:
                scanned += 1
                outcome = session.get(CareInterventionOutcome, intervention.id)
                if outcome is None:
                    outcome = CareInterventionRepository._ensure_outcome_in_session(
                        session, intervention, cutoff
                    )
                    created += 1
                if CareEffectivenessService._match_followups(
                    session, intervention, outcome, cutoff
                ):
                    updated += 1
            session.flush()
        return {"scanned": scanned, "created": created, "updated": updated}
