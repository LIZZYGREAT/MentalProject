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
from app.integrations.feishu.receiver_process import _ExpectedLarkShutdownFilter
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


class ControlledDeviceFlow:
    def __init__(self, close_started, close_allowed, close_finished, unblock_start):
        self.close_started = close_started
        self.close_allowed = close_allowed
        self.close_finished = close_finished
        self.unblock_start = unblock_start

    async def close(self):
        self.close_started.set()
        try:
            while not self.close_allowed.is_set():
                await asyncio.sleep(0.01)
        finally:
            self.close_finished.set()
            self.unblock_start.set()


class DeviceFlowFakeChannel:
    """Model the SDK background loop and its private DeviceFlow client."""

    def __init__(
        self,
        close_started,
        close_allowed,
        close_finished,
        public_stop_called,
        **_kwargs,
    ):
        self.is_ready = False
        self._start_unblocked = threading.Event()
        self._bg_loop = asyncio.new_event_loop()
        self._bg_thread = threading.Thread(
            target=self._bg_loop.run_forever,
            name="fake-lark-channel-bg",
            daemon=True,
        )
        self._device_flow = ControlledDeviceFlow(
            close_started,
            close_allowed,
            close_finished,
            self._start_unblocked,
        )
        self.public_stop_called = public_stop_called

    def on(self, name, _handler):
        assert name == "message"

    def start(self):
        self._bg_thread.start()
        self.is_ready = True
        self._start_unblocked.wait(5.0)

    def stop(self):
        self.public_stop_called.set()
        self._start_unblocked.set()
        self._bg_loop.call_soon_threadsafe(self._bg_loop.stop)
        self._bg_thread.join(2.0)
        self._bg_loop.close()


class PendingBackgroundTaskFakeChannel(DeviceFlowFakeChannel):
    """Expose an untracked SDK-loop task that must be cancelled and gathered."""

    def __init__(self, task_started, task_cleaned, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.task_started = task_started
        self.task_cleaned = task_cleaned

    async def _background_task(self):
        self.task_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.task_cleaned.set()

    def start(self):
        self._bg_thread.start()
        asyncio.run_coroutine_threadsafe(
            self._background_task(), self._bg_loop
        )
        assert self.task_started.wait(5.0)
        self.is_ready = True
        self._start_unblocked.wait(5.0)


def receiver_exits_after_ready(
    _app_id, _app_secret, output_queue, _stop_event, _channel_factory, *_args
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


def test_gateway_start_forwards_events_and_stop_cleans_receiver(caplog):
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
        for cycle in range(3):
            await gateway.start()
            assert gateway.is_running
            process = gateway._process
            if cycle == 0:
                event = await asyncio.wait_for(queue.get(), timeout=2)
                assert event.event_id == "msg-channel"
            await gateway.stop()
            assert not gateway.is_running
            assert process is not None and process.exitcode == 0

    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    ready_records = [
        record.getMessage() for record in caplog.records
        if "feishu_receiver_ready" in record.getMessage()
    ]
    assert ready_records
    assert all("receiver_pid=" in message for message in ready_records)
    assert not any(
        "feishu_receiver_graceful_stop_timed_out" in record.getMessage()
        for record in caplog.records
    )


def test_lark_normal_close_is_filtered_only_during_shutdown():
    shutdown_in_progress = threading.Event()
    log_filter = _ExpectedLarkShutdownFilter(shutdown_in_progress)

    def record(message):
        return __import__("logging").LogRecord(
            "Lark", __import__("logging").ERROR, __file__, 1, message, (), None
        )

    normal_close = record(
        "receive message loop exit, err: sent 1000 (OK); "
        "then received 1000 (OK) bye"
    )
    assert log_filter.filter(normal_close)

    shutdown_in_progress.set()
    assert not log_filter.filter(normal_close)
    assert log_filter.filter(
        record("receive message loop exit, err: received 1006 (abnormal closure)")
    )
    assert log_filter.filter(record("endpoint discovery failed with status 500"))


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


def test_gateway_precloses_sdk_device_flow_before_public_stop():
    context = multiprocessing.get_context("spawn")
    close_started = context.Event()
    close_allowed = context.Event()
    close_finished = context.Event()
    public_stop_called = context.Event()
    database = memory_database()
    gateway = FeishuGateway(
        "cli_test",
        "secret",
        IdentityService(database, BindingRepository(database)),
        BotEventRepository(database),
        asyncio.Queue(maxsize=2),
        channel_factory=partial(
            DeviceFlowFakeChannel,
            close_started,
            close_allowed,
            close_finished,
            public_stop_called,
        ),
        process_context=context,
        start_timeout_seconds=5,
        stop_timeout_seconds=3,
        device_flow_close_timeout_seconds=1,
        channel_sdk_version="1.2.0",
    )

    async def scenario():
        await gateway.start()
        stop_task = asyncio.create_task(gateway.stop())
        assert await asyncio.to_thread(close_started.wait, 2.0)
        assert not public_stop_called.is_set()
        close_allowed.set()
        await stop_task
        assert close_finished.is_set()
        assert public_stop_called.is_set()
        assert not gateway.is_running

    asyncio.run(scenario())


def test_gateway_drains_untracked_sdk_background_tasks_before_loop_close():
    context = multiprocessing.get_context("spawn")
    close_started = context.Event()
    close_allowed = context.Event()
    close_finished = context.Event()
    public_stop_called = context.Event()
    task_started = context.Event()
    task_cleaned = context.Event()
    close_allowed.set()
    database = memory_database()
    gateway = FeishuGateway(
        "cli_test",
        "secret",
        IdentityService(database, BindingRepository(database)),
        BotEventRepository(database),
        asyncio.Queue(maxsize=2),
        channel_factory=partial(
            PendingBackgroundTaskFakeChannel,
            task_started,
            task_cleaned,
            close_started,
            close_allowed,
            close_finished,
            public_stop_called,
        ),
        process_context=context,
        start_timeout_seconds=5,
        stop_timeout_seconds=3,
        device_flow_close_timeout_seconds=1,
        channel_sdk_version="1.2.0",
    )

    async def scenario():
        await gateway.start()
        process = gateway._process
        await gateway.stop()
        assert task_started.is_set()
        assert task_cleaned.is_set()
        assert close_finished.is_set()
        assert public_stop_called.is_set()
        assert process is not None and process.exitcode == 0

    asyncio.run(scenario())


def test_gateway_reports_device_flow_close_timeout_as_shutdown_error():
    context = multiprocessing.get_context("spawn")
    close_started = context.Event()
    close_allowed = context.Event()
    close_finished = context.Event()
    public_stop_called = context.Event()
    database = memory_database()
    gateway = FeishuGateway(
        "cli_test",
        "secret",
        IdentityService(database, BindingRepository(database)),
        BotEventRepository(database),
        asyncio.Queue(maxsize=2),
        channel_factory=partial(
            DeviceFlowFakeChannel,
            close_started,
            close_allowed,
            close_finished,
            public_stop_called,
        ),
        process_context=context,
        start_timeout_seconds=5,
        stop_timeout_seconds=3,
        device_flow_close_timeout_seconds=0.1,
        channel_sdk_version="1.2.0",
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        unhandled = []
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            await gateway.start()
            with pytest.raises(FeishuReceiverError, match="DeviceFlow close timed out"):
                await gateway.stop()
            assert close_started.is_set()
            assert close_finished.is_set()
            assert public_stop_called.is_set()
            assert not gateway.is_running
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


def test_queue_full_keeps_durable_event_recoverable(caplog):
    database = memory_database()
    queue = asyncio.Queue(maxsize=1)
    identity = IdentityService(database, BindingRepository(database))
    events = BotEventRepository(database)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    assert gateway.accept_payload(payload("evt-first")) is True
    second = payload("evt-second")
    second["event"]["message"]["message_id"] = "msg-2"
    with caplog.at_level("WARNING"):
        assert gateway.accept_payload(second) is True
    assert [item.event_id for item in events.recoverable()] == [
        "evt-first",
        "evt-second",
    ]
    messages = [record.getMessage() for record in caplog.records]
    assert any("feishu_event_queue_full" in message for message in messages)
    assert all("user" not in message for message in messages)


def test_normal_ingress_logs_safe_event_metadata(caplog):
    database = memory_database()
    queue = asyncio.Queue(maxsize=1)
    gateway = FeishuGateway(
        "cli_test",
        "secret-value",
        IdentityService(database, BindingRepository(database)),
        BotEventRepository(database),
        queue,
    )
    inbound = payload("evt-log")
    inbound["event"]["message"]["content"] = '{"text":"private-message-text"}'

    with caplog.at_level("INFO"):
        assert gateway.accept_payload(inbound) is True

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "feishu_gateway_event_accepted" in message
        and "event_id=evt-log" in message
        and "chat_type=p2p" in message
        for message in messages
    )
    combined = " ".join(messages)
    assert "private-message-text" not in combined
    assert "secret-value" not in combined
    assert "ou_test" not in combined
    assert "oc_test" not in combined
