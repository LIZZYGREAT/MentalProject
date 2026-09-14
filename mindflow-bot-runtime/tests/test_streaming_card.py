import asyncio
import json
from types import SimpleNamespace

from app.integrations.feishu.client import FeishuClient
from app.integrations.feishu.streaming_card import (
    ANSWER_ELEMENT_ID,
    FeishuStreamingCardSession,
    streaming_answer_card,
)


class RecordingCardClient:
    def __init__(self):
        self.operations = []

    def update_card_element_content(
        self, card_id, element_id, content, sequence
    ):
        self.operations.append(("update", card_id, element_id, content, sequence))

    def finish_streaming_card(self, card_id, sequence):
        self.operations.append(("close", card_id, sequence))


def test_streaming_session_uses_cumulative_content_strict_sequence_and_coalescing():
    client = RecordingCardClient()
    session = FeishuStreamingCardSession(
        client=client,
        card_id="card-1",
        message_id="message-1",
        visible_content="正在搜索公开网页…",
        update_interval_ms=10,
        min_update_chars=5,
        max_update_interval_ms=30,
    )

    async def scenario():
        await session.set_progress("正在整理结果…")
        assert session.answer_visible_chars == 0
        await session.update("答")
        await session.update("答案")
        await session.update("答案完整")
        assert session.answer_visible_chars == 0
        await session.update("答案完整开始")
        await session.finalize("答案完整开始，最终结束。")

    asyncio.run(scenario())

    updates = [operation for operation in client.operations if operation[0] == "update"]
    sequences = [operation[-1] for operation in client.operations]
    answer_updates = [operation[3] for operation in updates[1:]]
    assert updates[0][3] == "正在整理结果…"
    assert answer_updates == ["答案完整开始", "答案完整开始，最终结束。"]
    assert all(
        newer.startswith(older)
        for older, newer in zip(answer_updates, answer_updates[1:])
    )
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert client.operations[-1][0] == "close"
    assert session.closed is True


def test_streaming_card_schema_has_one_noninteractive_markdown_element():
    card = streaming_answer_card("正在整理结果…")

    assert card["schema"] == "2.0"
    assert card["config"]["streaming_mode"] is True
    assert card["header"]["title"]["content"] == "MindFlow"
    assert card["body"]["elements"] == [{
        "tag": "markdown",
        "element_id": ANSWER_ELEMENT_ID,
        "content": "正在整理结果…",
    }]


def test_failed_close_remains_retryable_and_uses_a_new_sequence():
    class FlakyCloseCardClient(RecordingCardClient):
        def __init__(self):
            super().__init__()
            self.close_attempts = 0

        def finish_streaming_card(self, card_id, sequence):
            self.operations.append(("close", card_id, sequence))
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("temporary close failure")

    client = FlakyCloseCardClient()
    session = FeishuStreamingCardSession(
        client=client,
        card_id="card-1",
        message_id="message-1",
    )

    async def scenario():
        try:
            await session.close()
        except RuntimeError:
            pass
        assert session.closed is False
        await session.close()

    asyncio.run(scenario())
    assert session.closed is True
    assert [operation[-1] for operation in client.operations] == [1, 2]


def test_feishu_client_uses_cardkit_preallocation_element_and_settings_endpoints():
    requests = []

    class Response:
        code = 0
        msg = "ok"
        data = {"card_id": "card-1"}

        @staticmethod
        def success():
            return True

    class SDK:
        def request(self, request):
            requests.append(request)
            return Response()

    client = FeishuClient("app", "secret", sdk_client=SDK())
    sent = []
    client._send_message = lambda chat_id, kind, content, **kwargs: (
        sent.append((chat_id, kind, content, kwargs)) or "message-1"
    )

    card_id = client.create_card_instance(streaming_answer_card())
    message_id = client.send_card_by_reference(
        "chat", card_id, message_uuid="stable"
    )
    client.update_card_element_content(card_id, ANSWER_ELEMENT_ID, "answer", 1)
    client.finish_streaming_card(card_id, 2)

    assert card_id == "card-1"
    assert message_id == "message-1"
    assert sent[0][2] == {"type": "card", "data": {"card_id": "card-1"}}
    assert requests[0].uri == "/open-apis/cardkit/v1/cards"
    assert json.loads(requests[0].body["data"])["config"]["streaming_mode"] is True
    assert requests[1].uri.endswith(
        f"/cards/card-1/elements/{ANSWER_ELEMENT_ID}/content"
    )
    assert requests[1].body == {"content": "answer", "sequence": 1}
    assert requests[2].uri.endswith("/cards/card-1/settings")
    assert requests[2].body["sequence"] == 2
    assert json.loads(requests[2].body["settings"])["config"]["streaming_mode"] is False


def test_send_reference_failure_still_closes_preallocated_streaming_mode():
    client = FeishuClient("app", "secret", sdk_client=SimpleNamespace())
    calls = []
    client.create_card_instance = lambda _card: "card-1"

    def fail_send(*_args, **_kwargs):
        raise RuntimeError("send failed")

    client.send_card_by_reference = fail_send
    client.finish_streaming_card = lambda card_id, sequence: calls.append(
        (card_id, sequence)
    )

    async def scenario():
        try:
            await client.start_streaming_card("chat")
        except RuntimeError:
            return
        raise AssertionError("expected failure")

    asyncio.run(scenario())
    assert calls == [("card-1", 1)]
