"""Policy layer that turns an authoritative answer into a delivery plan."""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import re
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
    ):
        self.sanitizer = sanitizer or MarkdownSanitizer()
        self.segmenter = segmenter or SemanticSegmenter()
        self.presentation_agent = presentation_agent
        self.presentation_agent_enabled = bool(presentation_agent_enabled)
        self.presentation_agent_min_chars = max(1, int(presentation_agent_min_chars))
        self.presentation_agent_timeout_seconds = max(
            0.1, float(presentation_agent_timeout_seconds)
        )
        self.presentation_agent_max_segments = max(
            1, min(int(presentation_agent_max_segments), 3)
        )

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

        agent_segments: tuple[str, ...] | None = None
        if self._should_use_presentation_agent(kind, authoritative):
            try:
                raw = await asyncio.wait_for(
                    self.presentation_agent.compose(
                        authoritative.text,
                        response_kind=kind,
                        has_card=False,
                        max_segments=self.presentation_agent_max_segments,
                    ),
                    timeout=self.presentation_agent_timeout_seconds,
                )
                agent_segments = self._validate_agent_output(
                    authoritative.text, raw
                )
            except Exception:
                agent_segments = None
        if agent_segments is not None:
            return self._plan(kind, agent_segments, False, True)

        deterministic = self.segmenter.segment(sanitized)
        if not deterministic and sanitized:
            deterministic = (sanitized,)
        return self._plan(kind, deterministic, False, False)

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

    def _should_use_presentation_agent(
        self, kind: ResponseKind, response: RuntimeResponse
    ) -> bool:
        return (
            kind == "analysis"
            and not response.safety_locked
            and len(response.text) >= self.presentation_agent_min_chars
            and self.presentation_agent_enabled
            and self.presentation_agent is not None
        )

    def _validate_agent_output(
        self, source_text: str, raw: Any
    ) -> tuple[str, ...]:
        if isinstance(raw, str):
            payload = json.loads(raw)
            raw = payload.get("segments") if isinstance(payload, dict) else None
        elif isinstance(raw, dict):
            raw = raw.get("segments")
        if not isinstance(raw, (list, tuple)):
            raise ValueError("segments must be a list")
        segments = tuple(str(item).strip() for item in raw)
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
            return "压力曲线已经整理好了，关键节点都在卡片里。"
        if len(text) > 180 or text.count("\n") > 3:
            return "结果已经整理在卡片里。"
        return text

    @staticmethod
    def _plan(
        kind: ResponseKind,
        texts: tuple[str, ...],
        use_cards: bool,
        presentation_agent_used: bool,
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
        )
