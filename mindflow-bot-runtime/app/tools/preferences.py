"""Structured interaction preference tools."""

from __future__ import annotations

from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.cards import (
    personalization_proposal_confirmation_card,
    preference_settings_card,
)


class InteractionPreferenceTools:
    def __init__(
        self,
        service: Any,
        presentations: Any = None,
        proposal_service: Any = None,
    ) -> None:
        self.service = service
        self.presentations = presentations
        self.proposal_service = proposal_service

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "interaction_preferences_get",
            "Get this participant's structured response-style preferences.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.get, effect="read", authorization_requirement="none",
        )
        registry.register(
            "interaction_preferences_update",
            "Stage a fixed review card for structured response style and assistant identity after an explicit lasting preference request. Natural-language interpretation is complete before this call; the tool itself does not persist preferences.",
            {
                "type": "object",
                "properties": {
                    "verbosity": {"type": "string", "enum": ["concise", "balanced", "detailed"]},
                    "tone": {"type": "string", "enum": ["neutral", "warm", "direct"]},
                    "suggestion_style": {"type": "string", "enum": ["ask_first", "light_suggestions", "proactive_suggestions"]},
                    "assistant_display_name": {"type": "string", "minLength": 1, "maxLength": 20},
                    "assistant_self_reference": {"type": "string", "minLength": 1, "maxLength": 20},
                },
                "minProperties": 1, "additionalProperties": False,
            },
            self.update,
            effect="proposal_stage",
            authorization_requirement="none",
        )
        registry.register(
            "support_preferences_update",
            "Stage a fixed review card for explicit support presentation and supportive follow-up preferences. This tool does not persist settings or record psychological state.",
            {
                "type": "object",
                "properties": {
                    "acknowledge_before_advice": {"type": "boolean"},
                    "ask_before_suggestion": {"type": "boolean"},
                    "max_suggestions": {"type": "integer", "minimum": 1, "maximum": 3},
                    "allow_supportive_follow_up": {"type": "boolean"},
                    "preferred_support_style": {"type": "string", "enum": ["gentle", "listening", "practical"]},
                },
                "minProperties": 1, "additionalProperties": False,
            },
            self.update_support,
            effect="proposal_stage",
            authorization_requirement="none",
        )
        registry.register(
            "preference_settings_show", "Generate the fixed interaction and support preference settings card for backend delivery. card_queued means generated, not delivered to Feishu.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.show_settings, effect="ui_effect", authorization_requirement="none",
        )

    def get(self, ctx: AgentContext, _args: dict[str, Any]):
        return {"ok": True, "interaction_preferences": self.service.get(ctx.participant_id)}

    def update(self, ctx: AgentContext, args: dict[str, Any]):
        style = {
            key: args[key]
            for key in ("verbosity", "tone", "suggestion_style")
            if key in args
        }
        identity = {
            key: args[key]
            for key in ("assistant_display_name", "assistant_self_reference")
            if key in args
        }
        try:
            proposal = self._proposals().stage_preferences(
                ctx.participant_id,
                domain="interaction_preferences",
                style_changes=style,
                identity_changes=identity,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": getattr(exc, "code", "invalid_interaction_preferences"),
                "reason_code": getattr(
                    exc, "code", "invalid_interaction_preferences"
                ),
                "message": str(exc),
            }
        return self._stage(ctx, proposal)

    def update_support(self, ctx: AgentContext, args: dict[str, Any]):
        try:
            proposal = self._proposals().stage_preferences(
                ctx.participant_id,
                domain="support_preferences",
                support_changes=args,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_support_preferences",
                "reason_code": "invalid_support_preferences",
                "message": str(exc),
            }
        return self._stage(ctx, proposal)

    def _proposals(self):
        if self.proposal_service is None:
            raise RuntimeError("personalization proposal service is unavailable")
        return self.proposal_service

    def _stage(self, ctx: AgentContext, proposal: dict[str, Any]):
        if self.presentations is None:
            raise RuntimeError("preference proposal presentation is unavailable")
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

    def show_settings(self, ctx: AgentContext, _args: dict[str, Any]):
        if self.presentations is None:
            raise RuntimeError("preference settings delivery is unavailable")
        self.presentations.stage_card(
            ctx.agent_run_id, preference_settings_card(self.service.get(ctx.participant_id))
        )
        return {
            "ok": True,
            "card_queued": True,
            "delivery_state": "queued_not_delivered",
        }
