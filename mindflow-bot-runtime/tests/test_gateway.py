import asyncio
from functools import partial
import inspect
import multiprocessing
import threading
import time
from types import SimpleNamespace

import pytest
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import (
    FeishuChannelMessageAdapter,
    FeishuEventParser,
    FeishuGateway,
    FeishuReceiverError,
    InvalidBotEvent,
)
from app.integrations.feishu.receiver_process import receiver_process_main
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


class BlockingFakeChannel:
    """Spawn-safe stand-in with the same blocking start/sync stop contract."""

    def __init__(self, **_kwargs):
        self.handler = None
        self.is_ready = False
        self._stopped = threading.Event()

    def on(self, name, handler):
        assert name == "message"
        self.handler = handler

    def start(self):
        self.is_ready = True
        self.handler(channel_message())
        self._stopped.wait(5.0)

    def stop(self):
        self._stopped.set()


class SlowStopFakeChannel:
    """Let start return before stop cleanup finishes to expose shutdown races."""

    def __init__(self, stop_started, stop_completed, **_kwargs):
        self.stop_started = stop_started
        self.stop_completed = stop_completed
        self.is_ready = False

    def on(self, name, _handler):
        assert name == "message"

    def start(self):
        self.is_ready = True
        assert self.stop_started.wait(5.0)

    def stop(self):
        self.stop_started.set()
        time.sleep(0.5)
        self.stop_completed.set()


class StopErrorFakeChannel:
    """Finish unblocking start but report a broken SDK teardown."""

    def __init__(self, **_kwargs):
        self.is_ready = False
        self._stopped = threading.Event()

    def on(self, name, _handler):
        assert name == "message"

    def start(self):
        self.is_ready = True
        self._stopped.wait(5.0)

    def stop(self):
        self._stopped.set()
        raise RuntimeError("STOP_BROKEN")


def receiver_exits_after_ready(
    _app_id, _app_secret, output_queue, _stop_event, _channel_factory
):
    output_queue.put({"kind": "ready"})
    time.sleep(0.1)


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


def test_gateway_start_forwards_events_and_stop_cleans_receiver():
    database = memory_database()
    queue = asyncio.Queue(maxsize=2)
    identity = IdentityService(database, BindingRepository(database))
    events = BotEventRepository(database)
    gateway = FeishuGateway(
        "cli_test",
        "secret",
        identity,
        events,
        queue,
        channel_factory=BlockingFakeChannel,
        start_timeout_seconds=5,
        stop_timeout_seconds=2,
    )

    async def scenario():
        await gateway.start()
        assert gateway.is_running
        event = await asyncio.wait_for(queue.get(), timeout=2)
        assert event.event_id == "msg-channel"
        await gateway.stop()
        assert not gateway.is_running
        await gateway.start()
        assert gateway.is_running
        await gateway.stop()
        assert not gateway.is_running

    asyncio.run(scenario())


def test_gateway_stop_waits_for_receiver_channel_cleanup():
    context = multiprocessing.get_context("spawn")
    stop_started = context.Event()
    stop_completed = context.Event()
    database = memory_database()
    gateway = FeishuGateway(
        "cli_test",
        "secret",
        IdentityService(database, BindingRepository(database)),
        BotEventRepository(database),
        asyncio.Queue(maxsize=2),
        channel_factory=partial(
            SlowStopFakeChannel, stop_started, stop_completed
        ),
        process_context=context,
        start_timeout_seconds=5,
        stop_timeout_seconds=3,
    )

    async def scenario():
        await gateway.start()
        await gateway.stop()
        assert stop_started.is_set()
        assert stop_completed.is_set()
        assert not gateway.is_running

    asyncio.run(scenario())


def test_gateway_stop_propagates_receiver_stop_failure_without_unretrieved_future():
    context = multiprocessing.get_context("spawn")
    database = memory_database()
    gateway = FeishuGateway(
        "cli_test",
        "secret",
        IdentityService(database, BindingRepository(database)),
        BotEventRepository(database),
        asyncio.Queue(maxsize=2),
        channel_factory=StopErrorFakeChannel,
        process_context=context,
        start_timeout_seconds=5,
        stop_timeout_seconds=3,
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        unhandled = []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            await gateway.start()
            process = gateway._process
            with pytest.raises(FeishuReceiverError, match="STOP_BROKEN"):
                await gateway.stop()
            assert process is not None and not process.is_alive()
            await asyncio.sleep(0)
            assert not any(
                "Future exception was never retrieved"
                in str(context.get("message") or "")
                for context in unhandled
            )
        finally:
            loop.set_exception_handler(old_handler)

    asyncio.run(scenario())


def test_receiver_abnormal_exit_is_detected():
    database = memory_database()
    gateway = FeishuGateway(
        "cli_test",
        "secret",
        IdentityService(database, BindingRepository(database)),
        BotEventRepository(database),
        asyncio.Queue(maxsize=2),
        receiver_target=receiver_exits_after_ready,
        start_timeout_seconds=5,
        stop_timeout_seconds=2,
    )

    async def scenario():
        await gateway.start()
        with pytest.raises(FeishuReceiverError, match="exited unexpectedly"):
            await asyncio.wait_for(gateway.wait_closed(), timeout=3)
        await gateway.stop()

    asyncio.run(scenario())


def test_backend_gateway_never_connects_lark_channel_on_its_running_loop():
    source = inspect.getsource(FeishuGateway.start)
    assert "lark_channel" not in source
    assert ".connect(" not in source
    receiver_source = inspect.getsource(receiver_process_main)
    assert "asyncio.new_event_loop()" in receiver_source
    assert receiver_source.index("asyncio.set_event_loop") < receiver_source.index(
        "from lark_channel import FeishuChannel"
    )


def test_bot_event_ipc_round_trip_has_no_sdk_objects():
    event = FeishuChannelMessageAdapter("cli_test").adapt(channel_message())
    payload = event.to_ipc_payload()
    assert payload == {
        "event_id": "msg-channel",
        "message_id": "msg-channel",
        "app_id": "cli_test",
        "open_id": "ou_channel",
        "chat_id": "oc_channel",
        "chat_type": "p2p",
        "message_type": "text",
        "text": "channel hello",
        "create_time": "2026-08-08T14:40:00+00:00",
    }
    assert type(event).from_ipc_payload(payload) == event


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
