import asyncio
from types import SimpleNamespace

from app.identity.service import IdentityService
from app.integrations.feishu.gateway import (
    FeishuChannelMessageAdapter,
    FeishuEventParser,
    FeishuGateway,
    InvalidBotEvent,
)
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


def channel_message(*, message_id="msg-channel", content_type="text", is_bot=False):
    return SimpleNamespace(
        id=message_id,
        create_time=1786200000000,
        conversation=SimpleNamespace(chat_id="oc_channel", chat_type="p2p"),
        sender=SimpleNamespace(open_id="ou_channel", is_bot=is_bot),
        raw={"message_id": message_id},
        raw_content_type=content_type,
        content_text="channel hello",
    )


def test_channel_adapter_keeps_stable_fields_and_filters_unsupported_messages():
    adapter = FeishuChannelMessageAdapter("cli_test")
    event = adapter.adapt(channel_message())
    assert event.event_id == "msg-channel"
    assert event.message_id == "msg-channel"
    assert event.open_id == "ou_channel"
    assert event.chat_id == "oc_channel"
    assert event.text == "channel hello"
    for message in (
        channel_message(content_type="image"),
        channel_message(is_bot=True),
    ):
        with __import__("pytest").raises(InvalidBotEvent):
            adapter.adapt(message)


def test_async_channel_lifecycle_does_not_reenter_the_running_loop():
    database = memory_database()
    queue = asyncio.Queue(maxsize=2)
    identity = IdentityService(database, BindingRepository(database))
    events = BotEventRepository(database)
    state = {"connected": False, "disconnected": False}

    class FakeChannel:
        def __init__(self, **_kwargs):
            self.handler = None

        def on(self, name, handler):
            assert name == "message"
            self.handler = handler

        async def connect(self):
            state["connected"] = True
            await self.handler(channel_message())

        async def disconnect(self):
            state["disconnected"] = True

    gateway = FeishuGateway(
        "cli_test",
        "secret",
        identity,
        events,
        queue,
        channel_factory=FakeChannel,
    )
    asyncio.run(gateway.start())
    assert state == {"connected": True, "disconnected": True}
    assert queue.get_nowait().event_id == "msg-channel"


def test_queue_full_keeps_durable_event_recoverable():
    database = memory_database()
    queue = asyncio.Queue(maxsize=1)
    identity = IdentityService(database, BindingRepository(database))
    events = BotEventRepository(database)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    assert gateway.accept_payload(payload("evt-first")) is True
    second = payload("evt-second")
    second["event"]["message"]["message_id"] = "msg-2"
    assert gateway.accept_payload(second) is True
    assert [item.event_id for item in events.recoverable()] == [
        "evt-first",
        "evt-second",
    ]
