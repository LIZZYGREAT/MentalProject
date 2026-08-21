"""Production allowlist of participant-bound care and calendar tools."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.cards import pressure_curve_card
from app.integrations.feishu.calendar import CalendarService
from app.repositories import (
    ObservationRepository,
    PredictionRepository,
    ProfileRepository,
    ForecastSnapshotRepository,
)
from app.services.prediction_service import PredictionService
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.presentation_service import PresentationOutbox
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


def _parse_datetime(value: Any, timezone_value: ZoneInfo) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("calendar time must be ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_value)
    return parsed.astimezone(timezone_value)


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
        forecast_coordinator: ForecastCoordinator | None = None,
        forecast_snapshots: ForecastSnapshotRepository | None = None,
        presentations: PresentationOutbox | None = None,
    ):
        self.profiles = profiles
        self.observations = observations
        self.predictions = predictions
        self.prediction_service = prediction_service
        self.calendar = calendar
        self.tokens = tokens
        self.timezone = ZoneInfo(timezone_name)
        self.forecast_coordinator = forecast_coordinator
        self.forecast_snapshots = forecast_snapshots
        self.presentations = presentations

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
            "care_get_pressure_curve",
            "Generate today's participant-bound forecast and queue a Feishu pressure/vitality curve card.",
            _empty_schema(),
            self.get_pressure_curve,
        )
        registry.register(
            "calendar_connection_status",
            "Return whether this participant has a usable Feishu calendar authorization.",
            _empty_schema(),
            self.calendar_connection_status,
        )
        registry.register(
            "calendar_list_calendars",
            "List calendars visible to this participant without exposing calendar identifiers.",
            _empty_schema(),
            self.list_calendars,
        )
        registry.register(
            "calendar_list_events",
            "List this participant's primary-calendar events in an explicit ISO 8601 time range.",
            {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "ISO 8601 start; timezone optional and defaults to the configured local timezone.",
                    },
                    "end_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "ISO 8601 end; timezone optional and defaults to the configured local timezone.",
                    },
                },
                "required": ["start_time", "end_time"],
                "additionalProperties": False,
            },
            self.list_calendar_events,
        )
        registry.register(
            "calendar_create_event",
            "Create one event in this participant's primary Feishu calendar after an explicit user request.",
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "minLength": 1, "maxLength": 200},
                    "start_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "ISO 8601 start; timezone optional and defaults to the configured local timezone.",
                    },
                    "end_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "ISO 8601 end; timezone optional and defaults to the configured local timezone.",
                    },
                    "description": {"type": "string", "maxLength": 1000},
                    "reminder_minutes": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1440,
                    },
                },
                "required": ["summary", "start_time", "end_time"],
                "additionalProperties": False,
            },
            self.create_calendar_event,
        )

    def get_today_context(self, ctx: AgentContext, _args: dict[str, Any]) -> dict[str, Any]:
        profile = self.profiles.current(ctx.participant_id)
        recent = self.observations.recent(ctx.participant_id, limit=1)
        prediction = self.predictions.latest(ctx.participant_id)
        latest_forecast = (
            self.forecast_snapshots.latest(
                ctx.participant_id, datetime.now(self.timezone).date()
            ) if self.forecast_snapshots is not None else None
        )
        return {
            "ok": True,
            "profile": _safe_profile(profile["profile"]) if profile else None,
            "profile_version": profile["version"] if profile else None,
            "latest_checkin": recent[0] if recent else None,
            "latest_assessment": prediction,
            "latest_forecast": latest_forecast,
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
        if self.forecast_coordinator is not None:
            result = await self.forecast_coordinator.ensure_forecast(
                ctx.participant_id, datetime.now(self.timezone).date(),
                "user_curve_request", refresh_calendar=True,
            )
            return {"ok": True, **result}
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

    async def get_pressure_curve(
        self, ctx: AgentContext, _args: dict[str, Any]
    ) -> dict[str, Any]:
        if self.forecast_coordinator is None:
            raise RuntimeError("forecast coordinator is unavailable")
        result = await self.forecast_coordinator.ensure_forecast(
            ctx.participant_id,
            datetime.now(self.timezone).date(),
            "user_curve_card_request",
            refresh_calendar=True,
        )
        curve = list(result.get("curve") or [])
        card = pressure_curve_card(
            curve, local_date=str(result.get("local_date") or datetime.now(self.timezone).date())
        )
        if self.presentations is None:
            raise RuntimeError("rich reply delivery is unavailable")
        self.presentations.stage_card(ctx.agent_run_id, card)
        stress_points = []
        for point in curve:
            try:
                stress_points.append(
                    (str(point.get("time") or ""), float(point.get("stress_0_10")))
                )
            except (TypeError, ValueError):
                continue
        peak_time, peak_value = max(stress_points, key=lambda item: item[1])
        return {
            "ok": True,
            "card_queued": True,
            "local_date": str(result.get("local_date") or ""),
            "point_count": len(stress_points),
            "predicted_peak": {"time": peak_time, "stress_0_10": round(peak_value, 2)},
            "calendar_degraded": bool(result.get("calendar_degraded")),
        }

    def calendar_connection_status(
        self, ctx: AgentContext, _args: dict[str, Any]
    ) -> dict[str, Any]:
        status = self.tokens.status(ctx.participant_id)
        scopes = set(status.get("scopes") or [])
        return {
            "ok": True,
            **status,
            "calendar_write_enabled": bool(
                "calendar:calendar.event:create" in scopes
                or "calendar:calendar" in scopes
            ),
        }

    async def list_calendars(
        self, ctx: AgentContext, _args: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            calendars = await self.calendar.list_calendars(ctx.participant_id)
        except PermissionError:
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        return {"ok": True, "calendars": calendars}

    async def list_calendar_events(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        start_time = _parse_datetime(args["start_time"], self.timezone)
        end_time = _parse_datetime(args["end_time"], self.timezone)
        try:
            events = await self.calendar.get_events(
                ctx.participant_id, start_time, end_time
            )
        except PermissionError:
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        return {
            "ok": True,
            "range": {"start_time": start_time.isoformat(), "end_time": end_time.isoformat()},
            "events": events,
        }

    async def create_calendar_event(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        start_time = _parse_datetime(args["start_time"], self.timezone)
        end_time = _parse_datetime(args["end_time"], self.timezone)
        try:
            event = await self.calendar.create_event(
                ctx.participant_id,
                summary=str(args["summary"]),
                description=str(args.get("description") or ""),
                start_time=start_time,
                end_time=end_time,
                reminder_minutes=args.get("reminder_minutes"),
                source_message_id=ctx.message_id,
            )
        except PermissionError:
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        return {"ok": True, "created": event}
