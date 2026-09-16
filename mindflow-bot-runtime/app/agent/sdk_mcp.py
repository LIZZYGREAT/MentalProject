"""Participant-bound in-process MCP facade over the reviewed ToolRegistry."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.presentation.contracts import (
    AgentActivityCallback,
    AgentActivityEvent,
    ExternalEvidenceSource,
    PresentationEvidence,
)


logger = logging.getLogger(__name__)


def _presentation_evidence(tool_name: str, value: Any) -> PresentationEvidence | None:
    if tool_name not in {
        "web_search",
        "web_read_result",
        "web_read_url",
        "web_read_url_chunk",
        "video_inspect_url",
        "video_read_transcript",
    }:
        return None
    if not isinstance(value, dict) or value.get("ok") is not True:
        return None
    kind = (
        "url_read"
        if tool_name.startswith("web_read_url") or tool_name.startswith("video_")
        else "search"
    )
    raw_sources: list[Any] = []
    if isinstance(value.get("sources"), list):
        raw_sources.extend(value["sources"])
    if isinstance(value.get("source"), dict):
        raw_sources.append(value["source"])
    if value.get("source_url"):
        raw_sources.append(value)
    sources = []
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        url = str(item.get("source_url") or item.get("url") or "").strip()
        if not url:
            continue
        sources.append(ExternalEvidenceSource(
            title=str(item.get("title") or "来源"),
            url=url,
            published_at=(
                str(item["published_at"]) if item.get("published_at") else None
            ),
            kind=kind,
        ))
    return PresentationEvidence(tuple(sources)) if sources else None


@dataclass
class TurnContextBinding:
    """Mutable only inside one participant session's serial turn processor."""

    current: AgentContext | None = None
    activity_callback: AgentActivityCallback | None = None

    def require(self) -> AgentContext:
        if self.current is None:
            raise RuntimeError("MindFlow tool called without an active backend context")
        return self.current

    async def emit(self, event: AgentActivityEvent) -> None:
        callback = self.activity_callback
        if callback is None:
            return
        try:
            await callback(event)
        except Exception:
            # Presentation feedback is best-effort and must never change tool
            # execution or the authoritative business result.
            logger.warning("agent_activity_callback_failed", exc_info=True)


def build_sdk_mcp_server(
    registry: ToolRegistry,
    binding: TurnContextBinding,
    *,
    sdk: Any,
) -> Any:
    """Create SDK tools without duplicating schemas or business handlers."""

    tools = []
    for spec in registry.specs_for(binding.current):

        async def execute(arguments: dict[str, Any], *, tool_name: str = spec.name):
            await binding.emit(
                AgentActivityEvent(kind="tool_started", tool_name=tool_name)
            )
            try:
                result = await registry.execute(
                    binding.require(), tool_name, arguments
                )
            except Exception:
                await binding.emit(
                    AgentActivityEvent(
                        kind="tool_failed",
                        tool_name=tool_name,
                        status="tool_exception",
                    )
                )
                raise
            await binding.emit(
                AgentActivityEvent(
                    kind=(
                        "tool_succeeded"
                        if result.status == "succeeded"
                        else "tool_failed"
                    ),
                    tool_name=tool_name,
                    status=result.status,
                    evidence=(
                        _presentation_evidence(tool_name, result.result)
                        if result.status == "succeeded"
                        else None
                    ),
                )
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result.result, ensure_ascii=False),
                    }
                ],
                "is_error": result.status not in {"succeeded"},
            }

        tools.append(sdk.tool(spec.name, spec.description, spec.parameters)(execute))
    return sdk.create_sdk_mcp_server(
        name="mindflow",
        version="1.0.0",
        tools=tools,
    )
