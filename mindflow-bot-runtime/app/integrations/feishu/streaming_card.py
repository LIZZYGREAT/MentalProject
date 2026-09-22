"""Validated-answer CardKit streaming session with cumulative updates."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from typing import Awaitable, Callable


ANSWER_ELEMENT_ID = "mindflow_answer"
_CARDKIT_ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")


def validate_cardkit_element_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not _CARDKIT_ELEMENT_ID.fullmatch(normalized):
        raise ValueError(
            "CardKit element_id must start with a letter, contain only letters, "
            "numbers, or underscores, and be at most 20 characters"
        )
    return normalized


def streaming_answer_card(content: str = "正在整理结果…") -> dict:
    element_id = validate_cardkit_element_id(ANSWER_ELEMENT_ID)
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "summary": {"content": "MindFlow"},
        },
        "header": {
            "title": {"tag": "plain_text", "content": "MindFlow"},
        },
        "body": {
            "elements": [{
                "tag": "markdown",
                "element_id": element_id,
                "content": str(content),
            }]
        },
    }


SequenceAllocator = Callable[[], int | Awaitable[int]]


class FeishuStreamingCardSession:
    def __init__(
        self,
        *,
        client,
        card_id: str,
        message_id: str,
        element_id: str = ANSWER_ELEMENT_ID,
        sequence: int = 0,
        visible_content: str = "",
        update_interval_ms: int = 120,
        min_update_chars: int = 30,
        max_update_interval_ms: int = 300,
        sequence_allocator: SequenceAllocator | None = None,
    ) -> None:
        self.client = client
        self.card_id = str(card_id)
        self.message_id = str(message_id)
        self.element_id = validate_cardkit_element_id(element_id)
        self.sequence = max(0, int(sequence))
        self.visible_content = str(visible_content)
        self.closed = False
        self.answer_started = False
        self.validated_content = ""
        self.answer_visible_chars = 0
        self.update_interval_seconds = max(0.01, int(update_interval_ms) / 1000)
        self.min_update_chars = max(1, int(min_update_chars))
        self.max_update_interval_seconds = max(
            self.update_interval_seconds,
            int(max_update_interval_ms) / 1000,
        )
        self.sequence_allocator = sequence_allocator
        self._last_update_at = 0.0
        self._pending_content = ""
        self._lock = asyncio.Lock()

    async def set_progress(self, content: str) -> None:
        async with self._lock:
            if self.closed or self.answer_started:
                return
            await self._write(str(content), force=True, validated=False)

    async def update(self, content: str) -> None:
        value = str(content)
        async with self._lock:
            if self.closed:
                raise RuntimeError("streaming card is closed")
            if self.answer_started and not value.startswith(self.validated_content):
                raise ValueError("streaming updates must contain cumulative validated content")
            self.answer_started = True
            self.validated_content = value
            self._pending_content = value
            elapsed = time.monotonic() - self._last_update_at
            if (
                len(value) - self.answer_visible_chars < self.min_update_chars
                and elapsed < self.max_update_interval_seconds
            ):
                return
            await self._write(value, force=False, validated=True)

    async def finalize(self, content: str) -> None:
        value = str(content)
        async with self._lock:
            if self.closed:
                return
            if self.answer_started and not value.startswith(self.validated_content):
                raise ValueError("final content must extend cumulative validated content")
            self.answer_started = True
            self.validated_content = value
            await self._write(value, force=True, validated=True)
            await self._close_locked()

    async def fail(self, message: str) -> None:
        async with self._lock:
            if self.closed:
                return
            try:
                await self._write(str(message), force=True, validated=False)
            finally:
                await self._close_locked()

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _write(self, content: str, *, force: bool, validated: bool) -> None:
        if content == self.visible_content:
            if validated:
                self.answer_visible_chars = len(content)
            return
        elapsed = time.monotonic() - self._last_update_at
        if not force and self._last_update_at and elapsed < self.update_interval_seconds:
            await asyncio.sleep(self.update_interval_seconds - elapsed)
        sequence = await self._next_sequence()
        await asyncio.to_thread(
            self.client.update_card_element_content,
            self.card_id,
            self.element_id,
            content,
            sequence,
        )
        self.visible_content = content
        self._pending_content = content
        self._last_update_at = time.monotonic()
        if validated:
            self.answer_visible_chars = len(content)

    async def _close_locked(self) -> None:
        if self.closed:
            return
        sequence = await self._next_sequence()
        await asyncio.to_thread(
            self.client.finish_streaming_card,
            self.card_id,
            sequence,
        )
        self.closed = True

    async def _next_sequence(self) -> int:
        if self.sequence_allocator is None:
            self.sequence += 1
            return self.sequence
        value = self.sequence_allocator()
        if inspect.isawaitable(value):
            value = await value
        next_sequence = int(value)
        if next_sequence <= self.sequence:
            raise ValueError("streaming sequence allocator must strictly increase")
        self.sequence = next_sequence
        return next_sequence
