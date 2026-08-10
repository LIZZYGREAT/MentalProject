import asyncio

from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuEventParser, FeishuGateway, InvalidBotEvent
from app.repositories import BindingRepository, BotEventRepository
from helpers import memory_database


def payload(event_id="evt-1", sender_type="user"):
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {
                "sender_type": sender_type,
                "sender_id": {"open_id": "ou_1"},
            },
            "message": {
                "message_id": "msg-1",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "message_type": "text",
                "content": '{"text":"hello"}',
                "create_time": "1786200000000",
            },
        },
    }


def test_event_normalization_self_filter_and_idempotency():
    database = memory_database()
    queue = asyncio.Queue(maxsize=2)
    identity = IdentityService(database, BindingRepository(database))
    events = BotEventRepository(database)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)

    assert gateway.accept_payload(payload()) is True
    assert gateway.accept_payload(payload()) is False
    event = queue.get_nowait()
    assert event.event_id == "evt-1"
    assert event.open_id == "ou_1"
    assert event.text == "hello"
    with __import__("pytest").raises(InvalidBotEvent):
        FeishuEventParser("cli_test").parse(payload("evt-bot", "bot"))


def test_received_event_keeps_enough_payload_for_restart_recovery():
    database = memory_database()
    queue = asyncio.Queue(maxsize=2)
    identity = IdentityService(database, BindingRepository(database))
    events = BotEventRepository(database)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)

    assert gateway.accept_payload(payload("evt-recover")) is True
    saved = events.recoverable()
    assert len(saved) == 1
    assert saved[0].event_id == "evt-recover"
    assert saved[0].open_id == "ou_1"
    assert saved[0].chat_id == "oc_1"
    assert saved[0].text == "hello"
