"""User-owned external LLM consent: one gate, one semantic.

The only authoritative source is participant_consents. Legacy researcher/CLI
flags (participants.external_llm_consent_at) are display/audit data during the
transition and never authorize external LLM processing; there is no admin or
researcher approval entry for this consent.
"""

from __future__ import annotations

from datetime import datetime
import uuid
from typing import Any

from app.repositories_consent import ParticipantConsentRepository


EXTERNAL_LLM_CONSENT_TYPE = "external_llm_processing"
# Bumping this version invalidates prior grants until the user re-consents.
EXTERNAL_LLM_CONSENT_VERSION = "1"


class ExternalLLMConsentRequired(PermissionError):
    """Raised fail-closed when the user has not consented to external LLM use."""


class ConsentService:
    def __init__(self, consents: ParticipantConsentRepository):
        self.consents = consents

    def is_active(self, participant_id: uuid.UUID) -> bool:
        return self.consents.is_active(
            participant_id,
            EXTERNAL_LLM_CONSENT_TYPE,
            EXTERNAL_LLM_CONSENT_VERSION,
        )

    def require_external_llm_consent(self, participant_id: uuid.UUID) -> None:
        """Fail closed unless this participant's own consent is active."""

        if not self.is_active(participant_id):
            raise ExternalLLMConsentRequired(
                "external LLM processing requires the participant's own consent"
            )

    def grant_external_llm_consent(
        self, participant_id: uuid.UUID, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Durable grant; returns only after the record is committed."""

        return self.consents.grant(
            participant_id,
            EXTERNAL_LLM_CONSENT_TYPE,
            EXTERNAL_LLM_CONSENT_VERSION,
            now=now,
        )

    def revoke_external_llm_consent(
        self, participant_id: uuid.UUID, *, now: datetime | None = None
    ) -> bool:
        """Immediate revoke; later external requests fail closed."""

        return self.consents.revoke(
            participant_id, EXTERNAL_LLM_CONSENT_TYPE, now=now
        )

    def status(self, participant_id: uuid.UUID) -> dict[str, Any]:
        current = self.consents.get_current(
            participant_id, EXTERNAL_LLM_CONSENT_TYPE
        )
        active = bool(
            current
            and current["status"] == "active"
            and current["consent_version"] == EXTERNAL_LLM_CONSENT_VERSION
        )
        return {
            "active": active,
            "consent_type": EXTERNAL_LLM_CONSENT_TYPE,
            "consent_version": (
                current["consent_version"] if current else EXTERNAL_LLM_CONSENT_VERSION
            ),
            "consented_at": current.get("consented_at") if current else None,
            "revoked_at": current.get("revoked_at") if current else None,
        }
