"""Structured interaction preference tools."""

from __future__ import annotations

from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.cards import preference_settings_card


class InteractionPreferenceTools:
    def __init__(self, service: Any, presentations: Any = None) -> None:
        self.service = service
        self.presentations = presentations

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "interaction_preferences_get",
            "Get this participant's structured response-style preferences.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            self.get, effect="read", authorization_requirement="none",
        )
        registry.register(
            "interaction_preferences_update",
            "Update structured response style and assistant identity after an explicit lasting preference request. Natural-language interpretation is complete before this call.",
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
            self.update, effect="internal_write", authorization_requirement="direct_request",
        )
        registry.register(
            "support_preferences_update",
            "Update explicit support presentation and supportive follow-up preferences. This never records psychological state.",
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
            self.update_support, effect="internal_write", authorization_requirement="direct_request",
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
            preferences = self.service.update_preferences(
                ctx.participant_id,
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
        return {"ok": True, "interaction_preferences": preferences}

    def set_rule(self, ctx: AgentContext, args: dict[str, Any]):
        """Legacy direct-call compatibility; intentionally not Agent-registered."""

        result = self.service.apply_rule(ctx.participant_id, args["rule"])
        return {"ok": bool(result["accepted"]) and not result.get("error"), **result}

    def update_support(self, ctx: AgentContext, args: dict[str, Any]):
        return {"ok": True, "support_preferences": self.service.update_support(ctx.participant_id, args)}

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
