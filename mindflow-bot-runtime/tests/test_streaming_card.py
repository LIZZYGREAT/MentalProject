import asyncio
import json
from types import SimpleNamespace

import pytest

from app.integrations.feishu.client import FeishuClient, FeishuSendError
from app.integrations.feishu.streaming_card import (
    ANSWER_ELEMENT_ID,
    FeishuStreamingCardSession,
    streaming_answer_card,
    validate_cardkit_element_id,
)
from app.presentation.streaming_boundaries import safe_stream_prefix_boundaries


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


def test_streaming_element_id_matches_feishu_contract():
    assert validate_cardkit_element_id(ANSWER_ELEMENT_ID) == ANSWER_ELEMENT_ID
    assert len(ANSWER_ELEMENT_ID) <= 20


@pytest.mark.parametrize(
    "element_id",
    ["1answer", "answer-with-dash", "answer space", "a" * 21, ""],
)
def test_client_rejects_invalid_cardkit_element_id_before_request(element_id):
    class SDK:
        def request(self, _request):
            raise AssertionError("invalid element_id must fail before HTTP")

    client = FeishuClient("app", "secret", sdk_client=SDK())
    with pytest.raises(ValueError, match="element_id"):
        client.update_card_element_content("card-1", element_id, "answer", 1)


def test_stream_prefix_never_splits_markdown_link_or_raw_url():
    text = (
        "先给结论：[已验证来源](https://example.com/very/long/article?q=1) "
        "随后参考 https://another.example.com/a/long/path?x=1 ，最后结束。"
    )
    boundaries = safe_stream_prefix_boundaries(text, max_updates=60, min_chars=1)
    link_start = text.index("[已验证来源]")
    link_end = text.index(")", link_start) + 1
    url_start = text.index("https://another")
    url_end = text.index(" ，", url_start)

    assert boundaries[-1] == len(text)
    assert all(not link_start < end < link_end for end in boundaries)
    assert all(not url_start < end < url_end for end in boundaries)


def test_stream_prefix_keeps_balanced_bold_marker_when_possible():
    text = "开头说明，**这一段强调内容不能在中间闪烁**，然后给出结论。"
    boundaries = safe_stream_prefix_boundaries(text, max_updates=60, min_chars=1)

    assert all(text[:end].count("**") % 2 == 0 for end in boundaries)


def test_stream_final_prefix_equals_full_compiled_text():
    text = "**结论**\n\n1. 第一项\n2. [来源](https://example.com/article)"
    boundaries = safe_stream_prefix_boundaries(text, max_updates=60, min_chars=1)

    assert text[:boundaries[-1]] == text
    assert len(boundaries) <= 60


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
    assert requests[1].body["content"] == "answer"
    assert requests[1].body["sequence"] == 1
    assert requests[1].body["uuid"] == client._cardkit_operation_uuid(
        "content", card_id, ANSWER_ELEMENT_ID, 1
    )
    assert requests[2].uri.endswith("/cards/card-1/settings")
    assert requests[2].body["sequence"] == 2
    assert json.loads(requests[2].body["settings"])["config"]["streaming_mode"] is False
    assert requests[2].body["uuid"] == client._cardkit_operation_uuid(
        "settings", card_id, "", 2
    )


def test_create_card_instance_parses_card_id_from_generic_raw_response():
    class Response:
        code = 0
        msg = "success"
        data = None
        raw = SimpleNamespace(
            content=b'{"code":0,"data":{"card_id":"7685996010229878000"},"msg":"success"}'
        )

        @staticmethod
        def success():
            return True

    class SDK:
        @staticmethod
        def request(_request):
            return Response()

    client = FeishuClient("app", "secret", sdk_client=SDK())

    assert client.create_card_instance(streaming_answer_card()) == "7685996010229878000"


def test_create_card_instance_does_not_require_response_data():
    class Response:
        code = 0
        msg = "success"
        data = None
        raw = SimpleNamespace(
            content=b'{"code":0,"data":{"card_id":"card-from-raw"},"msg":"success"}'
        )

        @staticmethod
        def success():
            return True

    client = FeishuClient("app", "secret", sdk_client=SimpleNamespace(
        request=lambda _request: Response()
    ))

    assert client.create_card_instance(streaming_answer_card()) == "card-from-raw"


@pytest.mark.parametrize(
    "raw_content",
    [
        b'{"code":0,"data":{},"msg":"success"}',
        b"not-json",
    ],
)
def test_create_card_instance_rejects_missing_or_invalid_raw_card_id(raw_content):
    class Response:
        code = 0
        msg = "success"
        data = None
        raw = SimpleNamespace(content=raw_content)

        @staticmethod
        def success():
            return True

    client = FeishuClient("app", "secret", sdk_client=SimpleNamespace(
        request=lambda _request: Response()
    ))

    with pytest.raises(FeishuSendError):
        client.create_card_instance(streaming_answer_card())


def test_cardkit_update_and_finalize_use_stable_uuid_for_retries():
    requests = []

    class Response:
        code = 0
        msg = "ok"

        @staticmethod
        def success():
            return True

    class SDK:
        def request(self, request):
            requests.append(request)
            return Response()

    client = FeishuClient("app", "secret", sdk_client=SDK())
    client.update_card_element_content("card-1", "answer", "body", 7)
    client.update_card_element_content("card-1", "answer", "body", 7)
    client.finish_streaming_card("card-1", 8)
    client.finish_streaming_card("card-1", 8)

    assert requests[0].body["uuid"] == requests[1].body["uuid"]
    assert requests[2].body["uuid"] == requests[3].body["uuid"]
    assert requests[0].body["uuid"] != requests[2].body["uuid"]


def test_cardkit_error_preserves_provider_code_request_id_and_operation():
    class Response:
        code = 230001
        msg = "permission denied"
        request_id = "req-cardkit-1"

        @staticmethod
        def success():
            return False

    class SDK:
        @staticmethod
        def request(_request):
            return Response()

    client = FeishuClient("app", "secret", sdk_client=SDK())

    with pytest.raises(FeishuSendError) as raised:
        client.update_card_element_content("card-1", "answer", "body", 1)

    assert raised.value.code == 230001
    assert raised.value.provider_request_id == "req-cardkit-1"
    assert raised.value.retryable is False
    assert raised.value.operation == "update_card_element_content"


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
