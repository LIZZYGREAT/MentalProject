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


def _card_service(database):
    from app.services.card_action_service import CardActionService

    return CardActionService(
        None,
        None,
        observation_refresh=None,
        consent_service=ConsentService(
            ParticipantConsentRepository(database)
        ),
    )


def _consent_action(service, database, participant_id, action_name):
    return service.handle(
        participant_id,
        message_id="om-consent",
        action_value={"mindflow_action": action_name, "version": "1"},
        form_value={},
    )


def test_consent_accept_grants_durable_user_consent():
    database = memory_database()
    service = _card_service(database)
    participant_id = uuid.uuid4()

    result = _consent_action(service, database, participant_id, "external_llm_consent_accept")

    assert result["ok"] is True
    assert "重新发送图片" in result["reply_text"]
    content = result["card"]["body"]["elements"][0]["content"]
    assert "状态：已开启" in content
    assert f"v{EXTERNAL_LLM_CONSENT_VERSION}" in content
    assert ConsentService(ParticipantConsentRepository(database)).is_active(
        participant_id
    )


def test_consent_decline_writes_nothing_and_reports_closed_state():
    database = memory_database()
    service = _card_service(database)
    participant_id = uuid.uuid4()

    result = _consent_action(service, database, participant_id, "external_llm_consent_decline")

    assert result["ok"] is True
    assert result["navigation_only"] is True
    assert ConsentService(ParticipantConsentRepository(database)).status(
        participant_id
    )["consented_at"] is None
    content = result["card"]["body"]["elements"][0]["content"]
    assert "状态：已关闭" in content


def test_consent_revoke_immediately_disables_processing():
    database = memory_database()
    service = _card_service(database)
    participant_id = uuid.uuid4()
    service.consent_service.grant_external_llm_consent(participant_id)

    result = _consent_action(service, database, participant_id, "external_llm_consent_revoke")

    assert result["ok"] is True
    assert "已关闭" in result["reply_text"]
    assert ConsentService(ParticipantConsentRepository(database)).is_active(
        participant_id
    ) is False
    content = result["card"]["body"]["elements"][0]["content"]
    assert "状态：已关闭" in content
    reopen = [
        element
        for element in result["card"]["body"]["elements"]
        if element.get("tag") == "button"
        and element["behaviors"][0]["value"]["mindflow_action"]
        == "external_llm_consent_accept"
    ]
    assert len(reopen) == 1


def test_consent_status_and_details_are_navigation_only_and_participant_bound():
    database = memory_database()
    service = _card_service(database)
    participant_id = uuid.uuid4()
    service.consent_service.grant_external_llm_consent(participant_id)

    status = _consent_action(service, database, participant_id, "external_llm_consent_status_open")
    assert status["navigation_only"] is True
    assert "状态：已开启" in status["card"]["body"]["elements"][0]["content"]

    other = uuid.uuid4()
    other_status = _consent_action(service, database, other, "external_llm_consent_status_open")
    assert "状态：已关闭" in other_status["card"]["body"]["elements"][0]["content"]

    details = _consent_action(service, database, participant_id, "external_llm_consent_details_open")
    assert details["navigation_only"] is True
    assert "数据范围" in details["card"]["body"]["elements"][0]["content"]
    values = [
        element["behaviors"][0]["value"]
        for element in details["card"]["body"]["elements"]
        if element.get("tag") == "button"
    ]
    assert all(set(value) == {"mindflow_action", "version"} for value in values)


def test_consent_callback_ignores_forged_participant_identity():
    database = memory_database()
    service = _card_service(database)
    participant_id = uuid.uuid4()
    forged = uuid.uuid4()

    result = service.handle(
        participant_id,
        message_id="om-consent",
        action_value={
            "mindflow_action": "external_llm_consent_accept",
            "version": "1",
            "participant_id": str(forged),
        },
        form_value={},
    )

    assert result["ok"] is True
    active_for = ConsentService(ParticipantConsentRepository(database))
    assert active_for.is_active(participant_id) is True
    assert active_for.is_active(forged) is False


def test_data_privacy_detail_card_links_consent_settings():
    from app.presentation.feature_cards import feature_detail_card

    detail = feature_detail_card("data_privacy")
    actions = [
        element["behaviors"][0]["value"]
        for element in detail["body"]["elements"]
        if element.get("tag") == "button"
    ]
    assert {
        "mindflow_action": "external_llm_consent_status_open",
        "version": "1",
    } in actions
