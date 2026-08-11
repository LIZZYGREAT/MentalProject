"""Participant-bound in-process MCP facade over the reviewed ToolRegistry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry


@dataclass
class TurnContextBinding:
    """Mutable only inside one participant session's serial turn processor."""

    current: AgentContext | None = None

    def require(self) -> AgentContext:
        if self.current is None:
            raise RuntimeError("MindFlow tool called without an active backend context")
        return self.current


def build_sdk_mcp_server(
    registry: ToolRegistry,
    binding: TurnContextBinding,
    *,
    sdk: Any,
) -> Any:
    """Create SDK tools without duplicating schemas or business handlers."""

    tools = []
    for spec in registry.specs:

        async def execute(arguments: dict[str, Any], *, tool_name: str = spec.name):
            result = await registry.execute(binding.require(), tool_name, arguments)
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
