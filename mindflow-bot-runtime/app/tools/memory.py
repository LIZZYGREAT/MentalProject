"""Explicit memory tools bound to the backend participant context."""

from __future__ import annotations

import uuid
from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.services.memory_service import MEMORY_TYPES


class MemoryTools:
    def __init__(self, memory: Any) -> None:
        self.memory = memory

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "memory_remember_explicit",
            "Remember durable participant context only when the current user explicitly says to remember it or sets a lasting preference.",
            {
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "enum": sorted(MEMORY_TYPES)},
                    "content": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["memory_type", "content"], "additionalProperties": False,
            },
            self.remember, effect="internal_write", authorization_requirement="direct_request",
        )
        registry.register(
            "memory_list", "List this participant's active explicit memories.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.list, effect="read", authorization_requirement="none",
        )
        registry.register(
            "memory_delete", "Delete one exact memory belonging to this participant.",
            {
                "type": "object",
                "properties": {"memory_id": {"type": "string", "format": "uuid"}},
                "required": ["memory_id"], "additionalProperties": False,
            },
            self.delete, effect="internal_write", authorization_requirement="direct_request",
        )
        registry.register(
            "memory_clear_all", "Clear all participant memories, without changing profile, calendar, observations, forecasts, or consent.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.clear_all, effect="internal_write", authorization_requirement="direct_request",
        )

    def remember(self, ctx: AgentContext, args: dict[str, Any]) -> dict:
        row = self.memory.remember_explicit(
            ctx.participant_id, memory_type=args["memory_type"], content=args["content"]
        )
        return {"ok": True, "memory": self._public(row)}

    def list(self, ctx: AgentContext, _args: dict[str, Any]) -> dict:
        return {"ok": True, "memories": [self._public(row) for row in self.memory.list(ctx.participant_id)]}

    def delete(self, ctx: AgentContext, args: dict[str, Any]) -> dict:
        deleted = self.memory.delete(ctx.participant_id, uuid.UUID(args["memory_id"]))
        return {"ok": deleted, "error": None if deleted else "memory_not_found"}

    def clear_all(self, ctx: AgentContext, _args: dict[str, Any]) -> dict:
        return {"ok": True, "deleted_count": self.memory.clear_all(ctx.participant_id)}

    @staticmethod
    def _public(row: dict) -> dict:
        return {key: row[key] for key in ("id", "memory_type", "content", "created_at")}
