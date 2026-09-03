import asyncio
import json
from types import SimpleNamespace

import pytest

from app.integrations.feishu.channel_card_ws_compat import install_card_ws_compat


def _frame(message_type, payload=b"{}"):
    import lark_channel.ws.client as ws

    frame = ws.Frame()
    frame.SeqID = 1
    frame.LogID = 1
    frame.service = 1
    frame.method = 1
    for key, value in (
        (ws.HEADER_MESSAGE_ID, "message-1"),
        (ws.HEADER_TRACE_ID, "trace-1"),
        (ws.HEADER_SUM, "1"),
        (ws.HEADER_SEQ, "0"),
        (ws.HEADER_TYPE, message_type.value),
    ):
        header = frame.headers.add()
        header.key = key
        header.value = str(value)
    frame.payload = payload
    return frame


def _client(result=None):
    import lark_channel.ws.client as ws

    received = []
    written = []
    client = ws.Client.__new__(ws.Client)
    client._event_handler = SimpleNamespace(
        _do_without_validation=lambda payload: received.append(payload) or result
    )
    client._combine = lambda *_args: None
    client._fmt_log = lambda template, *args: template.format(*args)

    async def write_message(data):
        written.append(data)

    client._write_message = write_message
    return client, received, written


def test_ws_card_frame_is_dispatched():
    import lark_channel.ws.client as ws

    install_card_ws_compat()
    client, received, _ = _client()
    asyncio.run(client._handle_data_frame(_frame(ws.MessageType.CARD, b"card")))
    assert received == [b"card"]


def test_ws_card_frame_writes_response():
    import lark_channel.ws.client as ws

    install_card_ws_compat()
    client, _, written = _client({"toast": {"content": "ok"}})
    asyncio.run(client._handle_data_frame(_frame(ws.MessageType.CARD, b"card")))
    assert len(written) == 1
    response_frame = ws.Frame()
    response_frame.ParseFromString(written[0])
    response = json.loads(response_frame.payload.decode("utf-8"))
    assert response["code"] == 200
    assert response.get("data")


def test_ws_event_frame_behavior_is_unchanged():
    import lark_channel.ws.client as ws

    install_card_ws_compat()
    client, received, written = _client()
    asyncio.run(client._handle_data_frame(_frame(ws.MessageType.EVENT, b"event")))
    assert received == [b"event"]
    assert len(written) == 1


def test_ws_unknown_frame_is_ignored():
    import lark_channel.ws.client as ws

    install_card_ws_compat()
    client, received, written = _client()
    asyncio.run(client._handle_data_frame(_frame(ws.MessageType.PING, b"control")))
    assert received == []
    assert written == []


def test_ws_card_compat_rejects_unknown_sdk_contract(monkeypatch):
    import lark_channel.ws.client as ws

    async def incompatible(self, frame, unexpected):
        return None

    monkeypatch.setattr(ws.Client, "_handle_data_frame", incompatible)
    with pytest.raises(
        RuntimeError, match="unsupported lark-channel-sdk CARD transport contract"
    ):
        install_card_ws_compat()
