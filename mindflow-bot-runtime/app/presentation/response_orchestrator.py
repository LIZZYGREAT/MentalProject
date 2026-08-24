"""Policy layer that turns an authoritative answer into a delivery plan."""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import re
import time
from typing import Any

from app.presentation.contracts import (
    ResponseKind,
    ResponsePlan,
    ResponseSegment,
    RuntimeResponse,
)
from app.presentation.markdown_sanitizer import MarkdownSanitizer
from app.presentation.presentation_agent import PresentationAgentProtocol
from app.presentation.semantic_segmenter import SemanticSegmenter


_TRANSACTIONAL_TOOLS = {
    "calendar_create_event",
    "calendar_update_event",
    "calendar_delete_event",
}
_ANALYSIS_TOOLS = {
    "care_run_today_assessment",
    "care_get_pressure_curve",
}
_CHECKIN_TOOLS = {"care_get_checkin_card"}
_MARKDOWN_MARKERS = re.compile(r"```|\*\*|^\s*#{1,6}\s+", re.MULTILINE)
_NUMBER = re.compile(
    r"(?<![\w])(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}:\d{2}|-?\d+(?:\.\d+)?%?)(?![\w])"
)


class ResponseOrchestrator:
    def __init__(
        self,
        *,
        sanitizer: MarkdownSanitizer | None = None,
        segmenter: SemanticSegmenter | None = None,
        presentation_agent: PresentationAgentProtocol | None = None,
        presentation_agent_enabled: bool = True,
        presentation_agent_min_chars: int = 600,
        presentation_agent_timeout_seconds: float = 4.0,
        presentation_agent_max_segments: int = 3,
        presentation_agent_mode: str = "adaptive",
        presentation_agent_max_pending_cleanups: int = 1,
    ):
        self.sanitizer = sanitizer or MarkdownSanitizer()
        self.segmenter = segmenter or SemanticSegmenter()
        self.presentation_agent = presentation_agent
        self.presentation_agent_enabled = bool(presentation_agent_enabled)
        self.presentation_agent_min_chars = max(1, int(presentation_agent_min_chars))
        self.presentation_agent_timeout_seconds = max(
            0.01, float(presentation_agent_timeout_seconds)
        )
        self.presentation_agent_max_segments = max(
            1, min(int(presentation_agent_max_segments), 3)
        )
        normalized_mode = str(presentation_agent_mode).strip().lower()
        if normalized_mode not in {"off", "adaptive", "always"}:
            raise ValueError("presentation_agent_mode must be off, adaptive, or always")
        self.presentation_agent_mode = normalized_mode
        self.presentation_agent_max_pending_cleanups = max(
            1, int(presentation_agent_max_pending_cleanups)
        )
        self._presentation_cleanups: set[asyncio.Task[Any]] = set()

    async def build_plan(
        self,
        response: RuntimeResponse | str,
        *,
        cards: list[object],
        used_tools: set[str],
    ) -> ResponsePlan:
        authoritative = self._coerce(response)
        tools = {str(name) for name in used_tools}
        kind = self._response_kind(authoritative, cards=cards, used_tools=tools)

        if kind in {"fixed", "error"} or authoritative.safety_locked:
            return ResponsePlan(
                kind=kind,
                full_text=authoritative.text,
                segments=(ResponseSegment(index=0, text=authoritative.text),),
                use_cards=bool(cards),
                presentation_agent_used=False,
            )

        sanitized = self.sanitizer.sanitize(authoritative.text)
        if kind == "transactional":
            return self._plan(kind, (sanitized,), False, False)

        if kind == "rich":
            companion = self._rich_companion(sanitized, tools)
            segments = (companion,) if companion else ()
            return self._plan(kind, segments, True, False)

        deterministic = self.segmenter.segment(sanitized)
        if not deterministic and sanitized:
            deterministic = (sanitized,)

        should_attempt, skipped_outcome = self._presentation_agent_decision(
            kind, authoritative, deterministic
        )
        agent_segments: tuple[str, ...] | None = None
        attempted = False
        outcome = skipped_outcome
        agent_latency_ms = 0.0
        if should_attempt:
            attempted = True
            started = time.monotonic()
            try:
                raw = await self._compose_with_hard_deadline(
                    authoritative.text, kind=kind
                )
            except TimeoutError:
                outcome = "timeout"
            except Exception:
                outcome = "agent_error"
            else:
                try:
                    raw_segments = self._parse_agent_output(raw)
                    sanitized_segments = tuple(
                        self.sanitizer.sanitize(segment)
                        for segment in raw_segments
                    )
                    agent_segments = self._validate_sanitized_agent_output(
                        authoritative.text, sanitized_segments
                    )
                    outcome = "used"
                except Exception:
                    outcome = "validation_reject"
            finally:
                agent_latency_ms = round((time.monotonic() - started) * 1000, 1)
        if agent_segments is not None:
            return self._plan(
                kind, agent_segments, False, True,
                presentation_agent_attempted=attempted,
                presentation_agent_outcome=outcome,
                presentation_agent_latency_ms=agent_latency_ms,
                presentation_cleanup_pending=len(self._presentation_cleanups),
            )

        return self._plan(
            kind, deterministic, False, False,
            presentation_agent_attempted=attempted,
            presentation_agent_outcome=outcome,
            presentation_agent_latency_ms=agent_latency_ms,
            presentation_cleanup_pending=len(self._presentation_cleanups),
        )

    async def _compose_with_hard_deadline(
        self, text: str, *, kind: ResponseKind
    ) -> Any:
        """Return at the deadline without waiting for slow SDK cancellation cleanup."""

        task = asyncio.create_task(
            self.presentation_agent.compose(
                text,
                response_kind=kind,
                has_card=False,
                max_segments=self.presentation_agent_max_segments,
            ),
            name="presentation-agent-compose",
        )
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=self.presentation_agent_timeout_seconds
            )
        except asyncio.CancelledError:
            task.cancel()
            self._track_cleanup(task)
            raise
        if task not in done:
            task.cancel()
            self._track_cleanup(task)
            raise TimeoutError("presentation agent deadline exceeded")
        return task.result()

    def _track_cleanup(self, task: asyncio.Task[Any]) -> None:
        self._presentation_cleanups.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._presentation_cleanups.discard(done)
            if done.cancelled():
                return
            try:
                done.exception()
            except BaseException:
                pass

        task.add_done_callback(finished)

    async def close(self) -> None:
        tasks = set(self._presentation_cleanups)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=0.5)
        close = getattr(self.presentation_agent, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass

    @staticmethod
    def _coerce(response: RuntimeResponse | str) -> RuntimeResponse:
        if isinstance(response, RuntimeResponse):
            return response
        return RuntimeResponse(text=str(response))

    def _response_kind(
        self,
        response: RuntimeResponse,
        *,
        cards: list[object],
        used_tools: set[str],
    ) -> ResponseKind:
        if response.safety_locked:
            return "error" if response.response_kind == "error" else "fixed"
        if used_tools & _TRANSACTIONAL_TOOLS:
            return "transactional"
        if cards:
            return "rich"
        if response.response_kind in {"analysis", "transactional", "rich"}:
            return response.response_kind
        if used_tools & _ANALYSIS_TOOLS or len(response.text) >= self.segmenter.min_total_chars:
            return "analysis"
        return "conversation"

    def _presentation_agent_decision(
        self,
        kind: ResponseKind,
        response: RuntimeResponse,
        deterministic: tuple[str, ...],
    ) -> tuple[bool, str]:
        if self.presentation_agent_mode == "off" or not self.presentation_agent_enabled:
            return False, "disabled"
        if (
            kind != "analysis"
            or response.safety_locked
            or len(response.text) < self.presentation_agent_min_chars
            or self.presentation_agent is None
        ):
            return False, "not_eligible"
        if len(self._presentation_cleanups) >= self.presentation_agent_max_pending_cleanups:
            return False, "cleanup_backpressure"
        if self.presentation_agent_mode == "adaptive" and self._within_delivery_envelope(
            deterministic
        ):
            return False, "skipped_adaptive"
        return True, "attempting"

    def _within_delivery_envelope(self, segments: tuple[str, ...]) -> bool:
        return (
            1 <= len(segments) <= self.presentation_agent_max_segments
            and all(0 < len(segment) <= self.segmenter.max_chars for segment in segments)
        )

    @staticmethod
    def _parse_agent_output(raw: Any) -> tuple[str, ...]:
        if isinstance(raw, str):
            payload = json.loads(raw)
            raw = payload.get("segments") if isinstance(payload, dict) else None
        elif isinstance(raw, dict):
            raw = raw.get("segments")
        if not isinstance(raw, (list, tuple)):
            raise ValueError("segments must be a list")
        return tuple(str(item) for item in raw)

    def _validate_sanitized_agent_output(
        self, source_text: str, segments: tuple[str, ...]
    ) -> tuple[str, ...]:
        segments = tuple(str(item).strip() for item in segments)
        if not 1 <= len(segments) <= self.presentation_agent_max_segments:
            raise ValueError("invalid segment count")
        if any(not item for item in segments):
            raise ValueError("empty segment")
        if any(len(item) > self.segmenter.max_chars for item in segments):
            raise ValueError("oversized segment")
        joined = "\n\n".join(segments)
        if _MARKDOWN_MARKERS.search(joined):
            raise ValueError("Markdown is not allowed")
        if Counter(_NUMBER.findall(source_text)) != Counter(_NUMBER.findall(joined)):
            raise ValueError("numeric values changed")
        return segments

    @staticmethod
    def _rich_companion(text: str, used_tools: set[str]) -> str:
        if not text:
            return ""
        if used_tools & _CHECKIN_TOOLS:
            return "可以直接在卡片里记录。"
        if "care_get_pressure_curve" in used_tools:
            return "今日压力曲线已生成，请查看卡片。"
        if len(text) > 180 or text.count("\n") > 3:
            return "结果已经整理在卡片里。"
        return text

    @staticmethod
    def _plan(
        kind: ResponseKind,
        texts: tuple[str, ...],
        use_cards: bool,
        presentation_agent_used: bool,
        *,
        presentation_agent_attempted: bool = False,
        presentation_agent_outcome: str = "not_eligible",
        presentation_agent_latency_ms: float = 0.0,
        presentation_cleanup_pending: int = 0,
    ) -> ResponsePlan:
        normalized = tuple(text for text in (str(item).strip() for item in texts) if text)
        return ResponsePlan(
            kind=kind,
            full_text="\n\n".join(normalized),
            segments=tuple(
                ResponseSegment(index=index, text=text)
                for index, text in enumerate(normalized)
            ),
            use_cards=use_cards,
            presentation_agent_used=presentation_agent_used,
            presentation_agent_attempted=presentation_agent_attempted,
            presentation_agent_outcome=presentation_agent_outcome,
            presentation_agent_latency_ms=presentation_agent_latency_ms,
            presentation_cleanup_pending=presentation_cleanup_pending,
        )
