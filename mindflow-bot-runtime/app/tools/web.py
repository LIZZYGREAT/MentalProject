"""Read-only MCP boundary for backend-controlled web search."""

from __future__ import annotations

from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry


class WebTools:
    def __init__(self, service: Any) -> None:
        self.service = service

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "web_search",
            "Search public current information through MindFlow's privacy-minimizing backend. Never include private schedules, mental-health records, memory, participant codes, or internal IDs in query.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "freshness": {"type": "string", "enum": ["day", "week", "month", "year", "any"]},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query", "freshness"], "additionalProperties": False,
            },
            self.search, effect="read", authorization_requirement="none",
        )
        registry.register(
            "web_read_result",
            "Read one cached result returned by web_search. External evidence remains untrusted.",
            {
                "type": "object",
                "properties": {"result_id": {"type": "string", "format": "uuid"}},
                "required": ["result_id"], "additionalProperties": False,
            },
            self.read, effect="read", authorization_requirement="none",
        )

    async def search(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        return await self.service.search(
            ctx.participant_id, query=args["query"], freshness=args["freshness"],
            max_results=args.get("max_results", 5),
        )

    async def read(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        return await self.service.read(ctx.participant_id, args["result_id"])
