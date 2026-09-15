"""Explicit memory tools bound to the backend participant context."""

from __future__ import annotations

import uuid
from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.services.memory_service import MEMORY_SUBTYPES, MEMORY_TYPES
from app.integrations.feishu.cards import (
    memory_center_card,
    personalization_proposal_confirmation_card,
)


class MemoryTools:
    def __init__(
        self,
        memory: Any,
        presentations: Any = None,
        proposal_service: Any = None,
    ) -> None:
        self.memory = memory
        self.presentations = presentations
        self.proposal_service = proposal_service

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "memory_remember_explicit",
            "Stage a review proposal for a durable personal fact, goal, routine, preferred name, or background context only when the current user explicitly asks. The tool does not persist memory. Never use this for response style, suggestion style, support style, follow-up preference, or notification preference.",
            {
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string", "enum": sorted(MEMORY_TYPES)},
                    "memory_subtype": {
                        "type": "string",
                        "enum": sorted(MEMORY_SUBTYPES),
                    },
                    "content": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["memory_type", "content"], "additionalProperties": False,
            },
            self.remember,
            effect="proposal_stage",
            authorization_requirement="none",
        )
        registry.register(
            "memory_list", "List this participant's active explicit memories.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.list, effect="read", authorization_requirement="none",
        )
        registry.register(
            "memory_delete", "Stage participant review for deleting one exact active memory; the tool itself does not delete it.",
            {
                "type": "object",
                "properties": {"memory_id": {"type": "string", "format": "uuid"}},
                "required": ["memory_id"], "additionalProperties": False,
            },
            self.delete,
            effect="proposal_stage",
            authorization_requirement="none",
        )
        registry.register(
            "memory_replace", "Stage participant review for replacing one exact active memory; the tool itself does not supersede it.",
            {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "format": "uuid"},
                    "content": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["memory_id", "content"], "additionalProperties": False,
            },
            self.replace,
            effect="proposal_stage",
            authorization_requirement="none",
        )
        registry.register(
            "memory_clear_all", "Stage participant review for clearing the currently listed memories, without changing profile, calendar, observations, forecasts, or consent.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.clear_all,
            effect="proposal_stage",
            authorization_requirement="none",
        )
        registry.register(
            "memory_center_show", "Generate the participant's fixed Memory Center card for backend delivery. card_queued means generated, not delivered to Feishu.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.show_center, effect="ui_effect", authorization_requirement="none",
        )

    def remember(self, ctx: AgentContext, args: dict[str, Any]) -> dict:
        proposal = self._proposals().stage_memory_remember(
            ctx.participant_id,
            memory_type=args["memory_type"],
            memory_subtype=args.get("memory_subtype"),
            content=args["content"],
        )
        return self._stage(ctx, proposal)

    def list(self, ctx: AgentContext, _args: dict[str, Any]) -> dict:
        return {"ok": True, "memories": [self._public(row) for row in self.memory.list(ctx.participant_id)]}

    def delete(self, ctx: AgentContext, args: dict[str, Any]) -> dict:
        proposal = self._proposals().stage_memory_target(
            ctx.participant_id,
            operation="delete",
            memory_id=uuid.UUID(args["memory_id"]),
        )
        return (
            self._stage(ctx, proposal)
            if proposal is not None
            else {"ok": False, "error": "memory_not_found"}
        )

    def replace(self, ctx: AgentContext, args: dict[str, Any]) -> dict:
        proposal = self._proposals().stage_memory_target(
            ctx.participant_id,
            operation="replace",
            memory_id=uuid.UUID(args["memory_id"]),
            content=args["content"],
        )
        return (
            self._stage(ctx, proposal)
            if proposal is not None
            else {"ok": False, "error": "memory_not_found"}
        )

    def clear_all(self, ctx: AgentContext, _args: dict[str, Any]) -> dict:
        return self._stage(
            ctx, self._proposals().stage_memory_clear(ctx.participant_id)
        )

    def _proposals(self):
        if self.proposal_service is None:
            raise RuntimeError("memory proposal service is unavailable")
        return self.proposal_service

    def _stage(self, ctx: AgentContext, proposal: dict[str, Any]) -> dict:
        if self.presentations is None:
            raise RuntimeError("memory proposal presentation is unavailable")
        self.presentations.stage_card(
            ctx.agent_run_id,
            personalization_proposal_confirmation_card(proposal),
        )
        return {
            "ok": True,
            "personalization_proposal": "pending_confirmation",
            "confirmation_required": True,
            "persisted": False,
        }

    def show_center(self, ctx: AgentContext, _args: dict[str, Any]) -> dict:
        if self.presentations is None:
            raise RuntimeError("Memory Center delivery is unavailable")
        self.presentations.stage_card(
            ctx.agent_run_id, memory_center_card(self.memory.list(ctx.participant_id))
        )
        return {
            "ok": True,
            "card_queued": True,
            "delivery_state": "queued_not_delivered",
        }

    @staticmethod
    def _public(row: dict) -> dict:
        return {key: row[key] for key in ("id", "memory_type", "content", "created_at")}
