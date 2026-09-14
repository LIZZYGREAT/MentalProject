import asyncio
import json

import httpx
import pytest

from app.services.web_search_service import (
    DeepSeekNativeSearchProvider,
    SearchUnavailable,
    WEB_SEARCH_TOOL_TYPE,
)


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.calls.append((url, headers, json))
        return self.response


class _SequenceClient:
    def __init__(self, responses, calls):
        self.responses = list(responses)
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.calls.append((url, headers, json))
        return self.responses.pop(0)


def _payload():
    return {
        "id": "msg_search_123",
        "content": [
            {
                "type": "server_tool_use",
                "name": "web_search",
            },
            {
                "type": "web_search_tool_result",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": "https://Example.com/a#fragment",
                        "title": "A",
                        "page_age": "2026-09-13",
                    },
                    {
                        "type": "web_search_result",
                        "url": "https://example.com/a",
                        "title": "Duplicate",
                    },
                ],
            },
            {
                "type": "text",
                "text": "A concise summary. https://hallucinated.example must not become a source.",
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }


def _provider(**overrides):
    options = {
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key": "deepseek-secret",
        "model": "deepseek-v4-flash",
        "timeout_seconds": 20,
        "max_uses": 3,
        "max_output_tokens": 1200,
    }
    options.update(overrides)
    return DeepSeekNativeSearchProvider(
        **options,
    )


def test_request_uses_derived_anthropic_endpoint_and_sanitized_query(monkeypatch):
    calls = []
    response = _Response(_payload())
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, calls),
    )

    result = asyncio.run(_provider().search("大学生压力研究", "month", 5))

    url, headers, body = calls[0]
    assert url == "https://api.deepseek.com/anthropic/v1/messages"
    assert headers["x-api-key"] == "deepseek-secret"
    assert "Authorization" not in headers
    assert headers["anthropic-version"] == "2023-06-01"
    assert body["tools"] == [{
        "type": WEB_SEARCH_TOOL_TYPE,
        "name": "web_search",
        "max_uses": 3,
    }]
    assert "大学生压力研究" in body["messages"][0]["content"]
    assert "last 30 days" in body["messages"][0]["content"]
    serialized = json.dumps(body, ensure_ascii=False)
    for private_value in ("participant_id", "participant_memory", "psychological_context", "calendar"):
        assert private_value not in serialized
    assert result.summary.startswith("A concise summary.")
    assert result.sources[0].url == "https://example.com/a"
    assert len(result.sources) == 1
    assert result.request_id == "msg_search_123"
    assert result.usage == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "provider_auth_failed"),
        (403, "provider_auth_failed"),
        (408, "provider_timeout"),
        (413, "provider_invalid_query"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
    ],
)
def test_http_failures_map_to_stable_reason_codes(monkeypatch, status, reason):
    response = _Response({})
    response.status_code = status
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(response, []),
    )

    with pytest.raises(SearchUnavailable, match=reason):
        asyncio.run(_provider().search("public topic", "any", 5))


@pytest.mark.parametrize(
    ("error_code", "reason"),
    [
        ("too_many_requests", "provider_rate_limited"),
        ("invalid_tool_input", "provider_invalid_query"),
        ("max_uses_exceeded", "provider_limit_exceeded"),
        ("query_too_long", "provider_invalid_query"),
        ("unavailable", "provider_unavailable"),
    ],
)
def test_structured_search_errors_are_not_exposed(monkeypatch, error_code, reason):
    payload = {
        "content": [{
            "type": "web_search_tool_result",
            "content": [{"type": "web_search_result_error", "error_code": error_code}],
        }]
    }
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(_Response(payload), []),
    )

    with pytest.raises(SearchUnavailable, match=reason):
        asyncio.run(_provider().search("public topic", "any", 5))


def test_missing_structured_sources_fails_even_when_summary_contains_a_url(monkeypatch):
    payload = {"content": [{"type": "text", "text": "See https://hallucinated.example"}]}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(_Response(payload), []),
    )

    with pytest.raises(SearchUnavailable, match="provider_no_sources"):
        asyncio.run(_provider().search("public topic", "any", 5))


def test_timeout_maps_without_returning_raw_provider_details(monkeypatch):
    class _TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "https://api.deepseek.com")
            raise httpx.ReadTimeout("private raw timeout details", request=request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _TimeoutClient())

    with pytest.raises(SearchUnavailable, match="provider_timeout") as exc_info:
        asyncio.run(_provider().search("public topic", "any", 5))
    assert "private raw" not in str(exc_info.value)


def _pause_payload(*content):
    return _Response({
        "id": "pause-message",
        "stop_reason": "pause_turn",
        "content": list(content),
    })


def _source_block(url="https://example.com/source"):
    return {
        "type": "web_search_tool_result",
        "content": [{
            "type": "web_search_result",
            "url": url,
            "title": "Structured source",
        }],
    }


def _complete_payload(*content):
    return _Response({
        "id": "complete-message",
        "stop_reason": "end_turn",
        "content": list(content),
    })


def test_pause_turn_continues_with_same_tools_and_raw_assistant_content(monkeypatch):
    server_tool_use = {"type": "server_tool_use", "name": "web_search"}
    pause = _pause_payload(server_tool_use)
    complete = _complete_payload(
        _source_block(),
        {"type": "text", "text": "Final search summary."},
    )
    calls = []
    client = _SequenceClient([pause, complete], calls)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    result = asyncio.run(_provider().search("public topic", "any", 5))

    assert result.summary == "Final search summary."
    assert result.sources[0].url == "https://example.com/source"
    assert len(calls) == 2
    assert calls[1][2]["messages"][:1] == calls[0][2]["messages"]
    assert calls[1][2]["messages"][1]["role"] == "assistant"
    assert calls[1][2]["messages"][1]["content"] is pause._payload["content"]
    assert calls[1][2]["tools"] == calls[0][2]["tools"]


def test_search_accumulates_text_across_continuations(monkeypatch):
    pause = _pause_payload(
        _source_block("https://example.com/first"),
        {"type": "text", "text": "First complete sentence."},
    )
    complete = _complete_payload(
        _source_block("https://example.com/final"),
        {"type": "text", "text": "Second complete sentence."},
    )
    client = _SequenceClient([pause, complete], [])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    result = asyncio.run(_provider().search("public topic", "any", 5))

    assert result.summary == "First complete sentence.\nSecond complete sentence."
    assert result.stop_reason == "end_turn"


def test_search_retries_once_after_truncation(monkeypatch):
    truncated = _Response({
        "id": "truncated",
        "stop_reason": "max_tokens",
        "content": [_source_block(), {"type": "text", "text": "cut off"}],
    })
    complete = _complete_payload(
        _source_block(),
        {"type": "text", "text": "Concise complete retry."},
    )
    calls = []
    client = _SequenceClient([truncated, complete], calls)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: client,
    )

    result = asyncio.run(_provider(
        retry_max_output_tokens=2400,
    ).search("public topic", "any", 5))

    assert result.summary == "Concise complete retry."
    assert [call[2]["max_tokens"] for call in calls] == [1200, 2400]
    assert "previous synthesis hit the output limit" in calls[1][2]["system"]


def test_search_rejects_final_max_tokens_as_verified(monkeypatch):
    truncated = _Response({
        "stop_reason": "max_tokens",
        "content": [_source_block(), {"type": "text", "text": "cut off"}],
    })
    calls = []
    client = _SequenceClient([truncated, truncated], calls)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: client,
    )

    with pytest.raises(SearchUnavailable, match="provider_truncated"):
        asyncio.run(_provider(
            retry_max_output_tokens=2400,
        ).search("public topic", "any", 5))
    assert len(calls) == 2


def test_search_never_cuts_summary_mid_sentence_silently():
    first = f"{'a' * 800}."
    second = f" {'b' * 500}."
    payload = {
        "stop_reason": "end_turn",
        "content": [_source_block(), {"type": "text", "text": first + second}],
    }

    result = _provider(summary_max_chars=1000)._parse_responses(
        [payload], max_results=5
    )

    assert result.summary == first
    assert result.summary.endswith(".")
    assert result.summary_truncated is True


def test_multiple_pause_turns_accumulate_sources_until_final_summary(monkeypatch):
    pause_one = _pause_payload({"type": "server_tool_use", "name": "web_search"})
    pause_two = _pause_payload(_source_block("https://example.com/from-pause"))
    complete = _complete_payload(
        _source_block("https://example.com/final"),
        {"type": "text", "text": "Completed after continuation."},
    )
    calls = []
    client = _SequenceClient([pause_one, pause_two, complete], calls)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    result = asyncio.run(_provider().search("public topic", "any", 5))

    assert len(calls) == 3
    assert {source.url for source in result.sources} == {
        "https://example.com/from-pause",
        "https://example.com/final",
    }


def test_pause_turn_limit_has_stable_reason_code(monkeypatch):
    pause = _pause_payload({"type": "server_tool_use", "name": "web_search"})
    calls = []
    client = _SequenceClient([pause, pause, pause, pause], calls)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    with pytest.raises(SearchUnavailable, match="provider_continuation_limit"):
        asyncio.run(_provider().search("public topic", "any", 5))
    assert len(calls) == 4


def test_pause_turn_without_intermediate_source_does_not_report_no_sources(monkeypatch):
    pause = _pause_payload({"type": "server_tool_use", "name": "web_search"})
    complete = _complete_payload(
        _source_block(),
        {"type": "text", "text": "Search completed after a pause."},
    )
    client = _SequenceClient([pause, complete], [])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    result = asyncio.run(_provider().search("public topic", "any", 5))

    assert result.sources


def test_final_summary_is_required_even_when_structured_source_exists(monkeypatch):
    payload = _complete_payload(_source_block())
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(payload, []),
    )

    with pytest.raises(SearchUnavailable, match="provider_invalid_response"):
        asyncio.run(_provider().search("public topic", "any", 5))
