"""Best-effort post-commit materialization of Stage-6 care outcomes."""

from __future__ import annotations

import logging
import uuid

from app.db import Database
from app.models import StateObservation
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
