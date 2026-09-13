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


def _provider():
    return DeepSeekNativeSearchProvider(
        base_url="https://api.deepseek.com/anthropic",
        api_key="deepseek-secret",
        model="deepseek-v4-flash",
        timeout_seconds=20,
        max_uses=3,
        max_output_tokens=1200,
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
    assert headers["Authorization"] == "Bearer deepseek-secret"
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
