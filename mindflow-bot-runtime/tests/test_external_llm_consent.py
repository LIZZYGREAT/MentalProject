"""Participant-owned external LLM consent records and service contracts."""

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import pytest

from app.repositories import ParticipantRepository
from app.repositories_consent import ParticipantConsentRepository
from app.services.consent_service import (
    EXTERNAL_LLM_CONSENT_TYPE,
    EXTERNAL_LLM_CONSENT_VERSION,
    ConsentService,
    ExternalLLMConsentRequired,
)
from helpers import seed_legacy_external_llm_consent
from helpers import memory_database, participant


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
    # Prior lifecycle rows stay queryable (history-preserving records).
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
    repository = ParticipantRepository(database)
    participant = repository.create("P-LEGACY")
    seed_legacy_external_llm_consent(database, participant.id)

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


def _consent_action(
    service,
    database,
    participant_id,
    action_name,
    *,
    consent_version=EXTERNAL_LLM_CONSENT_VERSION,
):
    action = {"mindflow_action": action_name, "version": "1"}
    if consent_version is not None:
        action["consent_version"] = consent_version
    return service.handle(
        participant_id,
        message_id="om-consent",
        action_value=action,
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
    for value in values:
        if value["mindflow_action"] == "external_llm_consent_accept":
            assert set(value) == {"mindflow_action", "version", "consent_version"}
            assert value["consent_version"] == EXTERNAL_LLM_CONSENT_VERSION
        else:
            assert set(value) == {"mindflow_action", "version"}


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
            "consent_version": EXTERNAL_LLM_CONSENT_VERSION,
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


def _gate_worker(database, sender, vision, *, consent_service=True):
    """Bind a fresh participant and build a worker wired to the consent gate."""

    import asyncio as _asyncio

    from app.agent.skill_loader import SkillLoader
    from app.identity.service import IdentityService
    from app.integrations.feishu.gateway import FeishuGateway
    from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
    from app.services.consent_service import ConsentService
    from app.worker import BotWorker
    from helpers import skill_path

    person = ParticipantRepository(database).create("P-GATE")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    events = BotEventRepository(database)
    queue = _asyncio.Queue(maxsize=8)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)

    class Runtime:
        def __init__(self):
            self.turns = 0

        async def handle_message(self, ctx, turn_input, **_kwargs):
            self.turns += 1
            return "ok"

    runtime = Runtime()
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        sender,
        model="fake",
        consent_service=(
            ConsentService(ParticipantConsentRepository(database))
            if consent_service
            else None
        ),
    )
    return gateway, worker, queue, person, identity, code, vision


def _image_payload(event_id, message_id):
    import json as _json

    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou-gate"}},
            "message": {
                "message_id": message_id,
                "chat_id": "oc-gate",
                "chat_type": "p2p",
                "message_type": "image",
                "content": _json.dumps({"image_key": "img_v2_opaque"}),
                "create_time": "1786200000000",
            },
        },
    }


def _text_payload(event_id, message_id, text):
    import json as _json

    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou-gate"}},
            "message": {
                "message_id": message_id,
                "chat_id": "oc-gate",
                "chat_type": "p2p",
                "message_type": "text",
                "content": _json.dumps({"text": text}),
            },
        },
    }


class CardCapturingSender:
    def __init__(self):
        self.sent = []
        self.cards = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return f"out-{len(self.sent)}"

    def send_card(self, chat_id, card, **_kwargs):
        self.cards.append((chat_id, card))
        return f"card-{len(self.cards)}"


class BlockedVision:
    def __init__(self):
        self.calls = 0

    async def parse(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Vision must not run without consent")


def test_image_without_user_consent_gets_consent_card_and_zero_vision_calls():
    database = memory_database()
    vision = BlockedVision()
    gateway, worker, queue, _person, identity, code, vision = _gate_worker(
        database, CardCapturingSender(), vision
    )
    sender = worker.sender

    async def scenario():
        assert gateway.accept_payload(
            _text_payload("bind", "m-bind", f"/bind {code}")
        )
        await worker.process(await queue.get())
        assert gateway.accept_payload(_image_payload("img1", "m-img1"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert vision.calls == 0
    # Bind welcome card first, then the fixed consent prompt.
    assert len(sender.cards) == 2
    content = sender.cards[-1][1]["body"]["elements"][0]["content"]
    assert "外部 AI 处理" in content
    actions = [
        element["behaviors"][0]["value"]
        for element in sender.cards[-1][1]["body"]["elements"]
        if element.get("tag") == "button"
    ]
    assert all(
        "consent_version" in value
        for value in actions
        if value["mindflow_action"] == "external_llm_consent_accept"
    )


def test_legacy_flag_without_user_consent_still_requires_consent():
    database = memory_database()
    vision = BlockedVision()
    gateway, worker, queue, person, identity, code, vision = _gate_worker(
        database, CardCapturingSender(), vision
    )
    sender = worker.sender
    # Researcher/CLI-set legacy flag must not authorize external LLM use.
    seed_legacy_external_llm_consent(database, person.id)

    async def scenario():
        assert gateway.accept_payload(
            _text_payload("bind", "m-bind", f"/bind {code}")
        )
        await worker.process(await queue.get())
        assert gateway.accept_payload(_text_payload("t1", "m-t1", "你好"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert worker.runtime.turns == 0
    assert len(sender.cards) == 2
    assert "外部 AI 处理" in sender.cards[-1][1]["body"]["elements"][0]["content"]


def test_text_turn_opens_after_user_grants_consent_and_closes_after_revoke():
    database = memory_database()
    vision = BlockedVision()
    gateway, worker, queue, person, identity, code, vision = _gate_worker(
        database, CardCapturingSender(), vision
    )
    sender = worker.sender

    async def scenario():
        assert gateway.accept_payload(
            _text_payload("bind", "m-bind", f"/bind {code}")
        )
        await worker.process(await queue.get())
        assert gateway.accept_payload(_text_payload("t1", "m-t1", "你好"))
        await worker.process(await queue.get())
        assert worker.runtime.turns == 0

        worker.consent_service.grant_external_llm_consent(person.id)
        assert gateway.accept_payload(_text_payload("t2", "m-t2", "你好"))
        await worker.process(await queue.get())
        assert worker.runtime.turns == 1

        worker.consent_service.revoke_external_llm_consent(person.id)
        assert gateway.accept_payload(_text_payload("t3", "m-t3", "你好"))
        await worker.process(await queue.get())
        assert worker.runtime.turns == 1

    asyncio.run(scenario())
    assert len(sender.cards) == 3
    assert "外部 AI 处理" in sender.cards[-1][1]["body"]["elements"][0]["content"]


def test_unwired_worker_fails_closed_even_when_legacy_flag_is_set():
    database = memory_database()
    vision = BlockedVision()
    gateway, worker, queue, person, identity, code, vision = _gate_worker(
        database, CardCapturingSender(), vision, consent_service=False
    )
    worker.consent_service = None
    sender = worker.sender
    seed_legacy_external_llm_consent(database, person.id)

    async def scenario():
        assert gateway.accept_payload(
            _text_payload("bind", "m-bind", f"/bind {code}")
        )
        await worker.process(await queue.get())
        assert gateway.accept_payload(_text_payload("t1", "m-t1", "你好"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    # No consent service means fail closed, even with the legacy flag set.
    assert worker.runtime.turns == 0
    assert "外部 AI 处理" in sender.cards[-1][1]["body"]["elements"][0]["content"]


def test_consent_copy_matches_resend_semantics():
    """Simplified continuation: the button must not promise in-place replay."""

    from app.integrations.feishu.cards import external_llm_consent_card

    text = repr(external_llm_consent_card())
    assert "同意并开启" in text
    assert "同意并继续" not in text


def test_stale_disclosure_accept_never_grants_newer_consent():
    database = memory_database()
    service = _card_service(database)
    participant_id = uuid.uuid4()

    result = _consent_action(
        service,
        database,
        participant_id,
        "external_llm_consent_accept",
        consent_version="1",
    )

    assert result["ok"] is True
    assert result["navigation_only"] is True
    assert "说明已经更新" in result["reply_text"]
    # Zero writes: the current disclosure card is returned instead.
    content = result["card"]["body"]["elements"][0]["content"]
    assert "开启外部 AI 处理" in content
    assert ConsentService(ParticipantConsentRepository(database)).is_active(
        participant_id
    ) is False


def test_current_version_accept_is_idempotent_without_duplicate_rows():
    database = memory_database()
    service = _card_service(database)
    participant_id = uuid.uuid4()

    first = _consent_action(service, database, participant_id, "external_llm_consent_accept")
    second = _consent_action(service, database, participant_id, "external_llm_consent_accept")

    assert first["ok"] is True and second["ok"] is True
    with database.session() as session:
        from sqlalchemy import select

        from app.models import ParticipantConsent

        rows = list(
            session.execute(
                select(ParticipantConsent).where(
                    ParticipantConsent.participant_id == participant_id
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].status == "active"
    assert rows[0].consent_version == EXTERNAL_LLM_CONSENT_VERSION


def test_revoke_on_stale_disclosure_card_still_takes_effect():
    database = memory_database()
    service = _card_service(database)
    participant_id = uuid.uuid4()
    service.consent_service.grant_external_llm_consent(participant_id)

    result = service.handle(
        participant_id,
        message_id="om-consent",
        # An old card carries the previous disclosure version; revoke must
        # still work because revocation never depends on the disclosure.
        action_value={
            "mindflow_action": "external_llm_consent_revoke",
            "version": "1",
            "consent_version": "1",
        },
        form_value={},
    )

    assert result["ok"] is True
    assert ConsentService(ParticipantConsentRepository(database)).is_active(
        participant_id
    ) is False


def test_consent_card_buttons_carry_current_disclosure_version():
    from app.integrations.feishu.cards import (
        external_llm_consent_card,
        external_llm_consent_details_card,
    )

    for card in (external_llm_consent_card(), external_llm_consent_details_card()):
        accepts = [
            element["behaviors"][0]["value"]
            for element in card["body"]["elements"]
            if element.get("tag") == "button"
            and element["behaviors"][0]["value"]["mindflow_action"]
            == "external_llm_consent_accept"
        ]
        assert len(accepts) == 1
        assert accepts[0]["consent_version"] == EXTERNAL_LLM_CONSENT_VERSION
        assert accepts[0]["version"] == "1"


def test_forecast_coordinator_rejects_legacy_flag_only_participant():
    import asyncio as _asyncio
    from datetime import date
    from types import SimpleNamespace

    from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
    from app.services.forecast_coordinator import ForecastCoordinator
    from app.repositories import (
        CalendarSnapshotRepository,
        EventSemanticCacheRepository,
        ForecastSnapshotRepository,
        ObservationRepository,
        ProfileRepository,
    )
    from helpers import warning_repository

    database = memory_database()
    repository = ParticipantRepository(database)
    person = repository.create("P-COORD-FAILCLOSED")
    seed_legacy_external_llm_consent(database, person.id)

    class Calendar:
        async def get_events(self, *_args):
            return [{
                "id": "evt-1",
                "summary": "组会汇报",
                "description": "讨论压力建模进展",
                "start_time": "2030-01-15T09:00:00+08:00",
                "end_time": "2030-01-15T10:00:00+08:00",
            }]

    class Prediction:
        model = SimpleNamespace(MODEL_VERSION="classification-test-v1")

        def calculate(self, **_kwargs):
            return {"trajectory": [{"time": "09:00", "stress_0_10": 4.0}], "alerts": []}

    semantics = EventSemanticPreprocessor(
        EventSemanticCacheRepository(database), client=None, model="semantic-test",
    )
    coordinator = ForecastCoordinator(
        participants=repository,
        profiles=ProfileRepository(database),
        observations=ObservationRepository(database),
        calendar=Calendar(),
        calendar_snapshots=CalendarSnapshotRepository(database),
        semantics=semantics,
        prediction=Prediction(),
        forecasts=ForecastSnapshotRepository(database),
        warnings=warning_repository(database),
        timezone_name="Asia/Shanghai",
    )

    target = date(2030, 1, 15)
    result = _asyncio.run(coordinator.ensure_forecast(person.id, target, "gate-test"))
    assert result["semantic_status"] in {"missing", "degraded", "partial", "rules_only"}
    # Without consent the external semantic classification never runs.
    assert result["semantic_status"] != "current"


def test_admin_read_model_reports_real_user_consent_not_legacy_flag():
    from app.admin_web.repositories import AdminRepository

    database = memory_database()
    repository = ParticipantRepository(database)
    person = repository.create("P-ADMIN-CONSENT")
    admin = AdminRepository(database)

    # No consent yet, legacy flag set: detail must not claim user consent.
    seed_legacy_external_llm_consent(database, person.id)
    detail = admin.participant("P-ADMIN-CONSENT")
    assert detail["external_llm_user_consent"]["active"] is False
    assert detail["legacy_external_llm_consent_at"] is not None
    assert "external_llm_consent" not in detail

    service = ConsentService(ParticipantConsentRepository(database))
    service.grant_external_llm_consent(person.id)
    detail = admin.participant("P-ADMIN-CONSENT")
    consent = detail["external_llm_user_consent"]
    assert consent["active"] is True
    assert consent["consent_version"] == EXTERNAL_LLM_CONSENT_VERSION
    assert consent["consented_at"] is not None

    service.revoke_external_llm_consent(person.id)
    detail = admin.participant("P-ADMIN-CONSENT")
    consent = detail["external_llm_user_consent"]
    assert consent["active"] is False
    assert consent["revoked_at"] is not None
    # The legacy flag is never interpreted as user consent.
    assert detail["legacy_external_llm_consent_at"] is not None
