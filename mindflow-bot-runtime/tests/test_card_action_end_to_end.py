import asyncio
import base64
from functools import partial
import json
import queue
from types import SimpleNamespace

import pytest

from app import main as app_main
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.integrations.feishu.receiver_process import receiver_process_main
from app.repositories import BindingRepository, BotEventRepository
from app.repositories_card_action import CardActionReceiptRepository
from app.services.card_action_service import CardActionService
from tests.helpers import memory_database, participant


def _frame(payload: bytes):
    import lark_channel.ws.client as ws

    frame = ws.Frame()
    frame.SeqID = 1
    frame.LogID = 1
    frame.service = 1
    frame.method = 1
    for key, value in (
        (ws.HEADER_MESSAGE_ID, "card-frame"),
        (ws.HEADER_TRACE_ID, "trace-card"),
        (ws.HEADER_SUM, "1"),
        (ws.HEADER_SEQ, "0"),
        (ws.HEADER_TYPE, ws.MessageType.CARD.value),
    ):
        header = frame.headers.add()
        header.key = key
        header.value = str(value)
    frame.payload = payload
    return frame


def _callback(payload):
    event = payload["event"]
    return SimpleNamespace(
        header=SimpleNamespace(event_id=payload["header"]["event_id"]),
        event=SimpleNamespace(
            token=event["token"],
            context=SimpleNamespace(**event["context"]),
            operator=SimpleNamespace(**event["operator"]),
            action=SimpleNamespace(**event["action"]),
        ),
    )


class RawCardFrameChannel:
    """SDK-shaped channel that drives a serialized CARD frame end to end."""

    def __init__(self, stop_event, raw_payload, written, **_kwargs):
        self.stop_event = stop_event
        self.raw_payload = raw_payload
        self.written = written
        self._dispatcher = None
        self.is_ready = False

    def on(self, name, _handler):
        assert name == "message"

    def _on_p2_card_action_trigger(self, _callback_value):
        raise AssertionError("receiver must install the synchronous ACK handler")

    def start(self):
        import lark_channel.ws.client as ws

        client = ws.Client.__new__(ws.Client)
        client._event_handler = SimpleNamespace(
            _do_without_validation=lambda payload: self._on_p2_card_action_trigger(
                _callback(json.loads(payload.decode("utf-8")))
            )
        )
        client._combine = lambda *_args: None
        client._fmt_log = lambda template, *args: template.format(*args)

        async def write_message(data):
            self.written.append(data)

        client._write_message = write_message
        self.is_ready = True
        asyncio.run(client._handle_data_frame(_frame(self.raw_payload)))
        self.stop_event.set()

    def stop(self):
        self.stop_event.set()


def _card_values(card):
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


@pytest.mark.parametrize(
    ("action_value", "expected_result_action"),
    [
        (
            {"mindflow_action": "feature_open", "version": "1", "feature_key": "checkin"},
            "feature_back",
        ),
        (
            {"mindflow_action": "feature_back", "version": "1", "feature_key": "overview"},
            "feature_open",
        ),
    ],
)
def test_feature_navigation_ws_card_action_end_to_end(
    action_value, expected_result_action, caplog
):
    caplog.set_level("INFO")
    database = memory_database()
    person = participant(database, f"E2E-{expected_result_action}")
    identity = IdentityService(database, BindingRepository(database))
    invite, _ = identity.create_invite(person.id)
    identity.bind(
        raw_token=invite,
        app_id="cli_test",
        open_id="ou-e2e",
        chat_id="oc-e2e",
    )
    raw_payload = json.dumps(
        {
            "header": {"event_id": f"event-{expected_result_action}"},
            "event": {
                "token": "callback-token",
                "operator": {"open_id": "ou-e2e"},
                "context": {
                    "open_message_id": "om-e2e",
                    "open_chat_id": "oc-e2e",
                },
                "action": {
                    "tag": "button",
                    "value": action_value,
                    "form_value": {},
                },
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    ipc = queue.Queue()
    stop_event = __import__("threading").Event()
    written = []
    receiver_process_main(
        "cli_test",
        "secret",
        ipc,
        stop_event,
        partial(RawCardFrameChannel, stop_event, raw_payload, written),
        card_action_enabled=True,
    )

    response_frame = _frame(b"")
    response_frame.ParseFromString(written[0])
    response = json.loads(response_frame.payload.decode("utf-8"))
    ack = json.loads(base64.b64decode(response["data"]).decode("utf-8"))
    assert ack == {"toast": {"type": "info", "content": "处理中…"}}

    updates = []

    class Sender:
        def update_card_from_callback(self, token, message_id, card):
            updates.append((token, message_id, card))

    handler = app_main._build_card_action_handler(
        identity,
        CardActionService(None, None, observation_refresh=None),
        Sender(),
        receipts=CardActionReceiptRepository(database),
    )
    gateway = FeishuGateway(
        "cli_test",
        "secret",
        identity,
        BotEventRepository(database),
        asyncio.Queue(maxsize=1),
        card_action_handler=handler,
    )

    async def consume():
        gateway._output_queue = ipc
        gateway._stopping = True
        gateway._closed = asyncio.get_running_loop().create_future()
        await gateway._consume_receiver_output()

    asyncio.run(consume())

    assert len(updates) == 1
    assert updates[0][0:2] == ("callback-token", "om-e2e")
    assert _card_values(updates[0][2])[0]["mindflow_action"] == expected_result_action
    logs = " ".join(record.getMessage() for record in caplog.records)
    assert "receiver_ack_ms=" in logs
    assert "ipc_queue_delay_ms=" in logs
    assert "business_latency_ms=" in logs
    assert "card_update_latency_ms=" in logs
    assert "total_card_action_latency_ms=" in logs
    assert "callback-token" not in logs
