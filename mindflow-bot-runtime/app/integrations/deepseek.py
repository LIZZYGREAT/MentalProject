"""Minimal OpenAI-compatible DeepSeek chat client."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import httpx


class DeepSeekError(RuntimeError):
    retryable = False


class DeepSeekTransientError(DeepSeekError):
    retryable = True


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Any


@dataclass(frozen=True)
class ChatResponse:
    content: str
    tool_calls: tuple[ToolCall, ...]
    assistant_message: dict[str, Any]


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 30.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def chat(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": messages,
                        "tools": tools,
                        "tool_choice": "auto",
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise DeepSeekTransientError("DeepSeek request failed") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise DeepSeekTransientError(f"DeepSeek HTTP {response.status_code}")
        if response.status_code >= 400:
            raise DeepSeekError(f"DeepSeek HTTP {response.status_code}")
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("DeepSeek response shape is invalid") from exc
        parsed_calls = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            raw_args = function.get("arguments", {})
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = raw_args
            parsed_calls.append(
                ToolCall(
                    id=str(item.get("id") or "tool_call"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )
        return ChatResponse(
            content=str(message.get("content") or ""),
            tool_calls=tuple(parsed_calls),
            assistant_message=message,
        )
