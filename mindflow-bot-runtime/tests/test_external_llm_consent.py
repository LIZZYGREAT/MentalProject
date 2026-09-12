"""Participant-owned external LLM consent records and service contracts."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from app.repositories_consent import ParticipantConsentRepository
from app.services.consent_service import (
    EXTERNAL_LLM_CONSENT_TYPE,
    EXTERNAL_LLM_CONSENT_VERSION,
    ConsentService,
    ExternalLLMConsentRequired,
)
from helpers import memory_database


def _service(database=None):
    database = database or memory_database()
    return ConsentService(ParticipantConsentRepository(database)), database


def test_grant_is_durable_and_activates_the_current_record():
    service, database = _service()
    participant_id = uuid.uuid4()

    assert service.is_active(participant_id) is False

    granted = service.grant_external_llm_consent(participant_id)

    assert granted["status"] == "active"
    assert granted["consent_type"] == EXTERNAL_LLM_CONSENT_TYPE
    assert granted["consent_version"] == EXTERNAL_LLM_CONSENT_VERSION
    # A brand-new repository instance sees the committed record.
    assert service.is_active(participant_id) is True
    assert ConsentService(
        ParticipantConsentRepository(database)
    ).is_active(participant_id) is True


def test_revoke_takes_effect_immediately_and_can_be_re_granted():
    service, _database = _service()
    participant_id = uuid.uuid4()
    service.grant_external_llm_consent(participant_id)

    assert service.revoke_external_llm_consent(participant_id) is True
    assert service.is_active(participant_id) is False
    assert service.status(participant_id)["active"] is False
    assert service.status(participant_id)["revoked_at"] is not None

    service.grant_external_llm_consent(participant_id)
    assert service.is_active(participant_id) is True


def test_revoke_without_grant_is_a_reported_noop():
    service, _database = _service()
    assert service.revoke_external_llm_consent(uuid.uuid4()) is False


def test_version_mismatch_counts_as_not_consented():
    service, database = _service()
    participant_id = uuid.uuid4()
    repository = ParticipantConsentRepository(database)
    repository.grant(
        participant_id,
        EXTERNAL_LLM_CONSENT_TYPE,
        "0-obsolete",
    )

    assert service.is_active(participant_id) is False
    assert service.status(participant_id)["active"] is False
    with pytest.raises(ExternalLLMConsentRequired):
        service.require_external_llm_consent(participant_id)


def test_require_fails_closed_until_granted_and_passes_after():
    service, _database = _service()
    participant_id = uuid.uuid4()
    with pytest.raises(ExternalLLMConsentRequired):
        service.require_external_llm_consent(participant_id)

    service.grant_external_llm_consent(participant_id)
    service.require_external_llm_consent(participant_id)


def test_current_record_is_the_latest_consent_not_the_first():
    service, database = _service()
    repository = ParticipantConsentRepository(database)
    participant_id = uuid.uuid4()
    first = service.grant_external_llm_consent(
        participant_id,
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    service.revoke_external_llm_consent(
        participant_id, now=datetime(2026, 9, 2, tzinfo=timezone.utc)
    )
    second = service.grant_external_llm_consent(
        participant_id,
        now=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )

    current = repository.get_current(participant_id, EXTERNAL_LLM_CONSENT_TYPE)
    assert current["id"] == second["id"]
    assert current["id"] != first["id"]
    # The revoked row stays as append-only history.
    history = current
    assert history["status"] == "active"


def test_grant_honours_explicit_time_for_deterministic_history():
    service, _database = _service()
    participant_id = uuid.uuid4()
    granted = service.grant_external_llm_consent(
        participant_id, now=datetime(2026, 9, 8, 16, 30, tzinfo=timezone.utc)
    )
    # SQLite returns naive UTC datetimes; compare the instant.
    consented_at = granted["consented_at"]
    if consented_at.tzinfo is None:
        consented_at = consented_at.replace(tzinfo=timezone.utc)
    assert consented_at == datetime(2026, 9, 8, 16, 30, tzinfo=timezone.utc)
    assert service.status(participant_id)["consented_at"] is not None


def test_legacy_flag_alone_never_activates_user_consent():
    service, database = _service()
    from app.repositories import ParticipantRepository

    participant = ParticipantRepository(database).create("P-LEGACY")
    repository = ParticipantRepository(database)
    repository.set_external_llm_consent(participant.id, allowed=True)

    assert service.is_active(participant.id) is False
    with pytest.raises(ExternalLLMConsentRequired):
        service.require_external_llm_consent(participant.id)
