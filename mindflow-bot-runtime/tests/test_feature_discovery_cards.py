"""Reviewed feature discovery cards and canonical in-place navigation."""

import asyncio
import json
import uuid

from app.agent.skill_loader import SkillLoader
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.presentation.feature_cards import (
    FEATURE_SPECS,
    build_feature_card,
    feature_detail_card,
    feature_overview_card,
    normalize_feature_key,
    onboarding_welcome_card,
    visible_feature_keys,
)
from app.presentation.onboarding import welcome_first_screen_text
from app.presentation.user_capabilities import help_text
from app.repositories import (
    AgentRunRepository,
    BindingRepository,
    BotEventRepository,
)
from app.services.card_action_service import CardActionService
from app.worker import BotWorker
from helpers import memory_database, participant, skill_path


class FakeRuntime:
    async def handle_message(self, ctx, turn_input, **_kwargs):
        return "ok"


class FakeSender:
    def __init__(self, *, fail_cards=False):
        self.sent = []
        self.cards = []
        self.fail_cards = fail_cards

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return f"out-{len(self.sent)}"

    def send_card(self, chat_id, card, **_kwargs):
        from app.integrations.feishu.client import FeishuSendError

        if self.fail_cards:
            raise FeishuSendError("card transport down", retryable=False)
        self.cards.append((chat_id, card))
        return f"card-{len(self.cards)}"


def card_action_values(card):
    values = []

    def walk(node):
        if isinstance(node, dict):
            if "mindflow_action" in node:
                values.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(card)
    return values


def test_every_feature_spec_renders_an_overview_and_detail_card():
    keys = visible_feature_keys()
    assert keys == tuple(FEATURE_SPECS)
    overview = feature_overview_card()
    actions = card_action_values(overview)
    assert [value["feature_key"] for value in actions] == list(keys)
    assert all(value["mindflow_action"] == "feature_open" for value in actions)
    for key in keys:
        detail = feature_detail_card(key)
        assert detail is not None
        back = card_action_values(detail)
        assert back == [{
            "mindflow_action": "feature_back",
            "version": "1",
            "feature_key": "overview",
        }]


def test_disabled_capability_hides_the_feature_everywhere():
    keys = visible_feature_keys({"daily_review_enabled": False})
    assert "daily_review" not in keys
    overview_actions = [
        value["feature_key"] for value in card_action_values(feature_overview_card(keys))
    ]
    assert "daily_review" not in overview_actions
    assert feature_detail_card("daily_review", keys) is None
    assert feature_detail_card("daily_review", visible_feature_keys()) is not None


def test_feature_key_normalization_rejects_unknown_values():
    assert normalize_feature_key("checkin") == "checkin"
    assert normalize_feature_key("overview") == "overview"
    assert normalize_feature_key("  calendar ") == "calendar"
    assert normalize_feature_key("made_up_feature") is None
    assert normalize_feature_key("") is None
    assert build_feature_card("made_up_feature") is None
    assert build_feature_card("overview") is not None


def test_welcome_card_only_navigates_and_never_mutates():
    card = onboarding_welcome_card()
    actions = card_action_values(card)
    assert [value["feature_key"] for value in actions] == [
        "checkin", "calendar", "course_schedule", "overview",
    ]
    assert all(value["mindflow_action"] == "feature_open" for value in actions)


def _card_service(database, **kwargs):
    return CardActionService(
        None,
        None,
        observation_refresh=None,
        **kwargs,
    )


def test_card_action_service_navigates_in_place_with_backend_enum():
    service = _card_service(memory_database())
    participant_id = uuid.uuid4()

    opened = service.handle(
        participant_id,
        message_id="om",
        action_value={
            "mindflow_action": "feature_open",
            "version": "1",
            "feature_key": "checkin",
        },
        form_value={},
    )
    assert opened["ok"] is True
    detail = card_action_values(opened["card"])
    assert detail[0]["mindflow_action"] == "feature_back"

    back = service.handle(
        participant_id,
        message_id="om",
        action_value={
            "mindflow_action": "feature_back",
            "version": "1",
            "feature_key": "overview",
        },
        form_value={},
    )
    assert back["ok"] is True
    assert card_action_values(back["card"])[0]["mindflow_action"] == "feature_open"


def test_card_action_service_rejects_invalid_feature_navigation():
    service = _card_service(memory_database())
    participant_id = uuid.uuid4()

    rejected = service.handle(
        participant_id,
        message_id="om",
        action_value={
            "mindflow_action": "feature_open",
            "version": "1",
            "feature_key": "self_invented_feature",
        },
        form_value={},
    )
    assert rejected == {"ok": False, "error": "unsupported_feature"}

    version = service.handle(
        participant_id,
        message_id="om",
        action_value={
            "mindflow_action": "feature_open",
            "version": "9",
            "feature_key": "checkin",
        },
        form_value={},
    )
    assert version == {"ok": False, "error": "unsupported_card_action_version"}

    hidden = _card_service(
        memory_database(),
        feature_capabilities={"daily_review_enabled": False},
    ).handle(
        participant_id,
        message_id="om",
        action_value={
            "mindflow_action": "feature_open",
            "version": "1",
            "feature_key": "daily_review",
        },
        form_value={},
    )
    assert hidden == {"ok": False, "error": "unsupported_feature"}


def payload(event_id, message_id, open_id, chat_id, text):
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": open_id}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def build_worker(database, identity, sender):
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=8)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        FakeRuntime(),
        sender,
        model="fake",
    )
    return gateway, worker, queue


def test_exact_help_words_send_the_deterministic_overview_card():
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    sender = FakeSender()
    gateway, worker, queue = build_worker(database, identity, sender)

    async def scenario():
        assert gateway.accept_payload(payload("b1", "m1", "ou", "oc", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("h1", "m2", "ou", "oc", "功能"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("h2", "m3", "ou", "oc", "你会什么？"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(sender.cards) == 3
    assert card_action_values(sender.cards[1][1])[0]["mindflow_action"] == (
        "feature_open"
    )
    assert sender.sent == []


def test_help_fast_path_falls_back_to_text_when_cards_are_unavailable():
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    sender = FakeSender(fail_cards=True)
    gateway, worker, queue = build_worker(database, identity, sender)

    async def scenario():
        assert gateway.accept_payload(payload("b1", "m1", "ou", "oc", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("h1", "m2", "ou", "oc", "帮助"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert sender.cards == []
    assert ("oc", help_text()) in sender.sent


def test_bind_success_sends_the_progressive_welcome_card():
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    sender = FakeSender()
    gateway, worker, queue = build_worker(database, identity, sender)

    async def scenario():
        assert gateway.accept_payload(payload("b1", "m1", "ou", "oc", f"/bind {code}"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(sender.cards) == 1
    assert card_action_values(sender.cards[0][1]) == card_action_values(
        onboarding_welcome_card()
    )


def test_bind_success_falls_back_to_the_durable_text_first_screen():
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    sender = FakeSender(fail_cards=True)
    gateway, worker, queue = build_worker(database, identity, sender)

    async def scenario():
        assert gateway.accept_payload(payload("b1", "m1", "ou", "oc", f"/bind {code}"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert sender.cards == []
    assert sender.sent == [("oc", welcome_first_screen_text())]
