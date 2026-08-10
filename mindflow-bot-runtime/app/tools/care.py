"""First production allowlist: five participant-bound care tools."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.calendar import CalendarService
from app.repositories import (
    ObservationRepository,
    PredictionRepository,
    ProfileRepository,
)
from app.services.prediction_service import PredictionService
from app.services.token_service import TokenRepository


def _empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _safe_profile(profile: dict[str, Any]) -> dict[str, Any]:
    blocked = {"token", "secret", "password", "student_no", "login_id"}

    def clean(value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "[truncated]"
        if isinstance(value, dict):
            return {
                str(key): clean(child, depth + 1)
                for key, child in value.items()
                if not any(term in str(key).lower() for term in blocked)
            }
        if isinstance(value, list):
            return [clean(item, depth + 1) for item in value[:100]]
        return value

    return clean(profile)


class CareTools:
    def __init__(
        self,
        profiles: ProfileRepository,
        observations: ObservationRepository,
        predictions: PredictionRepository,
        prediction_service: PredictionService,
        calendar: CalendarService,
        tokens: TokenRepository,
        timezone_name: str,
    ):
        self.profiles = profiles
        self.observations = observations
        self.predictions = predictions
        self.prediction_service = prediction_service
        self.calendar = calendar
        self.tokens = tokens
        self.timezone = ZoneInfo(timezone_name)

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "care_get_today_context",
            "Return this participant's current profile, recent check-in, and latest assessment.",
            _empty_schema(),
            self.get_today_context,
        )
        registry.register(
            "care_record_checkin",
            "Record a momentary non-clinical pressure/vitality check-in.",
            {
                "type": "object",
                "properties": {
                    "stress": {"type": "number", "minimum": 0, "maximum": 10},
                    "energy": {"type": "number", "minimum": 0, "maximum": 10},
                    "activity": {"type": "string", "minLength": 1, "maxLength": 120},
                    "stress_event_since_last": {"type": "boolean"},
                    "event_ongoing": {"type": "boolean"},
                },
                "required": [
                    "stress",
                    "energy",
                    "activity",
                    "stress_event_since_last",
                    "event_ongoing",
                ],
                "additionalProperties": False,
            },
            self.record_checkin,
        )
        registry.register(
            "care_get_recent_state",
            "Return this participant's recent recorded check-ins without running a model.",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20}
                },
                "additionalProperties": False,
            },
            self.get_recent_state,
        )
        registry.register(
            "care_run_today_assessment",
            "Run the reviewed MindFlow stress/vitality model for today.",
            _empty_schema(),
            self.run_assessment,
        )
        registry.register(
            "care_get_support",
            "Return a brief optional support suggestion based on recorded state.",
            {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "maxLength": 300},
                },
                "additionalProperties": False,
            },
            self.get_support,
        )
        registry.register(
            "calendar_connection_status",
            "Return whether this participant has a usable Feishu calendar authorization.",
            _empty_schema(),
            self.calendar_connection_status,
        )

    def get_today_context(self, ctx: AgentContext, _args: dict[str, Any]) -> dict[str, Any]:
        profile = self.profiles.current(ctx.participant_id)
        recent = self.observations.recent(ctx.participant_id, limit=1)
        prediction = self.predictions.latest(ctx.participant_id)
        return {
            "ok": True,
            "profile": _safe_profile(profile["profile"]) if profile else None,
            "profile_version": profile["version"] if profile else None,
            "latest_checkin": recent[0] if recent else None,
            "latest_assessment": prediction,
        }

    def record_checkin(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        observation_id = self.observations.add(
            ctx.participant_id,
            "checkin",
            {
                "stress_0_10": float(args["stress"]),
                "energy_0_10": float(args["energy"]),
                "activity": str(args["activity"]),
                "stress_event_since_last": bool(args["stress_event_since_last"]),
                "event_ongoing": bool(args["event_ongoing"]),
            },
            source_message_id=ctx.message_id,
        )
        return {
            "ok": True,
            "observation_id": str(observation_id),
            "recorded": {"stress": args["stress"], "energy": args["energy"]},
        }

    def get_recent_state(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "observations": self.observations.recent(
                ctx.participant_id, limit=int(args.get("limit", 5))
            ),
        }

    async def run_assessment(
        self, ctx: AgentContext, _args: dict[str, Any]
    ) -> dict[str, Any]:
        profile_row = self.profiles.current(ctx.participant_id)
        profile = profile_row["profile"] if profile_row else {}
        observations = self.observations.recent(ctx.participant_id, limit=50)
        now = datetime.now(self.timezone)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            calendar_events = await self.calendar.get_events(
                ctx.participant_id, day_start, day_start + timedelta(days=1)
            )
            degraded = False
        except Exception:
            calendar_events = []
            degraded = True
        result = await asyncio.to_thread(
            self.prediction_service.run,
            participant_id=ctx.participant_id,
            profile_version=profile_row["version"] if profile_row else None,
            profile=profile,
            observations=observations,
            calendar_events=calendar_events,
            calendar_degraded=degraded,
            local_date=day_start.date().isoformat(),
            source_message_id=ctx.message_id,
        )
        return {"ok": True, **result}

    def get_support(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        recent = self.observations.recent(ctx.participant_id, limit=1)
        payload = recent[0]["payload"] if recent else {}
        stress = float(payload.get("stress_0_10", 0.0))
        energy = float(payload.get("energy_0_10", 10.0))
        if stress >= 7:
            suggestion = "如果方便，可以先离开当前任务两分钟，缓慢呼吸并确认下一件最小可做的事。"
        elif energy <= 3:
            suggestion = "如果方便，可以先补水并安排一小段不带任务的休息。"
        else:
            suggestion = "可以按当前节奏继续；如果状态变化，随时做一次简短打卡。"
        return {
            "ok": True,
            "support_type": "optional_brief_support",
            "suggestion": suggestion,
            "context_acknowledged": bool(args.get("context")),
        }

    def calendar_connection_status(
        self, ctx: AgentContext, _args: dict[str, Any]
    ) -> dict[str, Any]:
        return {"ok": True, **self.tokens.status(ctx.participant_id)}
