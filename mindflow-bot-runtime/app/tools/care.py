"""Production allowlist of participant-bound care and calendar tools."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.cards import daily_checkin_card, pressure_curve_card
from app.integrations.feishu.calendar import CalendarService, build_recurrence_rule
from app.repositories import (
    ObservationRepository,
    PredictionRepository,
    ProfileRepository,
    ForecastSnapshotRepository,
    LearnedProfileRepository,
)
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.observation_forecast_refresh import ObservationForecastRefreshService
from app.services.pressure_curve_service import (
    HistoricalForecastNotFoundError,
    PressureCurveService,
)
from app.services.presentation_service import (
    IMAGE_KEY_PLACEHOLDER,
    PresentationOutbox,
)
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


def _recurrence_schema_properties() -> dict[str, Any]:
    return {
        "recurrence_frequency": {
            "type": "string",
            "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"],
        },
        "recurrence_interval": {"type": "integer", "minimum": 1, "maximum": 99},
        "recurrence_weekdays": {
            "type": "array",
            "items": {"type": "string", "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]},
            "uniqueItems": True,
            "maxItems": 7,
        },
        "recurrence_count": {"type": "integer", "minimum": 1, "maximum": 999},
        "recurrence_until": {"type": "string", "format": "date-time"},
    }


def _recurrence_from_args(args: dict[str, Any], timezone_value: ZoneInfo) -> str | None:
    frequency = args.get("recurrence_frequency")
    if frequency is None:
        return None
    until = (
        _parse_datetime(args["recurrence_until"], timezone_value)
        if args.get("recurrence_until")
        else None
    )
    return build_recurrence_rule(
        str(frequency),
        interval=int(args.get("recurrence_interval", 1)),
        weekdays=list(args.get("recurrence_weekdays") or []),
        count=args.get("recurrence_count"),
        until=until,
    )


class CareTools:
    def __init__(
        self,
        profiles: ProfileRepository,
        observations: ObservationRepository,
        predictions: PredictionRepository,
        calendar: CalendarService,
        tokens: TokenRepository,
        timezone_name: str,
        forecast_coordinator: ForecastCoordinator,
        forecast_snapshots: ForecastSnapshotRepository | None = None,
        presentations: PresentationOutbox | None = None,
        learned_profiles: LearnedProfileRepository | None = None,
        pressure_curves: PressureCurveService | None = None,
        observation_refresh: ObservationForecastRefreshService | None = None,
    ):
        self.profiles = profiles
        self.observations = observations
        self.predictions = predictions
        self.calendar = calendar
        self.tokens = tokens
        self.timezone = ZoneInfo(timezone_name)
        self.forecast_coordinator = forecast_coordinator
        self.forecast_snapshots = forecast_snapshots
        self.presentations = presentations
        self.learned_profiles = learned_profiles
        self.pressure_curves = pressure_curves or (
            PressureCurveService(
                forecast_coordinator,
                timezone_name=timezone_name,
            )
            if forecast_coordinator is not None
            else None
        )
        self.observation_refresh = observation_refresh

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
            "Run today's reviewed MindFlow model using its active versioned state definition.",
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
            "Generate a participant-bound pressure forecast for a requested local date and queue its pressure-only curve card.",
            {
                "type": "object",
                "properties": {
                    "local_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Requested local calendar date in YYYY-MM-DD. Omit for today.",
                    }
                },
                "additionalProperties": False,
            },
            self.get_pressure_curve,
        )
        registry.register(
            "care_get_checkin_card",
            "Queue the reviewed Feishu daily-state questionnaire card for this participant.",
            _empty_schema(),
            self.get_checkin_card,
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
                    **_recurrence_schema_properties(),
                },
                "required": ["summary", "start_time", "end_time"],
                "additionalProperties": False,
            },
            self.create_calendar_event,
        )
        registry.register(
            "calendar_update_event",
            "Update one exact event in this participant's primary calendar after confirmation.",
            {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 200},
                    "start_time": {"type": "string", "format": "date-time"},
                    "end_time": {"type": "string", "format": "date-time"},
                    "description": {"type": "string", "maxLength": 1000},
                    "reminder_minutes": {"type": "integer", "minimum": 0, "maximum": 1440},
                    "clear_recurrence": {"type": "boolean"},
                    **_recurrence_schema_properties(),
                },
                "required": ["event_id"],
                "additionalProperties": False,
            },
            self.update_calendar_event,
        )
        registry.register(
            "calendar_delete_event",
            "Delete one exact event from this participant's primary calendar after explicit confirmation.",
            {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "confirmed": {"type": "boolean", "const": True},
                },
                "required": ["event_id", "confirmed"],
                "additionalProperties": False,
            },
            self.delete_calendar_event,
        )

    def get_today_context(self, ctx: AgentContext, _args: dict[str, Any]) -> dict[str, Any]:
        profile = self.profiles.current(ctx.participant_id)
        learned_profile = (
            self.learned_profiles.current(ctx.participant_id)
            if self.learned_profiles is not None else None
        )
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
            "profile_layers": {
                "precedence": ["system_defaults", "learned", "explicit"],
                "explicit": profile,
                "learned": learned_profile,
            },
            "latest_checkin": recent[0] if recent else None,
            "latest_assessment": prediction,
            "latest_forecast": latest_forecast,
        }

    def record_checkin(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        if self.observation_refresh is None:
            raise RuntimeError("observation forecast refresh service is unavailable")
        write = self.observations.add_with_status(
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
        self.observation_refresh.on_observation_committed(
            participant_id=ctx.participant_id,
            observed_at=write.observed_at,
            created=write.created,
        )
        return {
            "ok": True,
            "observation_id": str(write.observation_id),
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
        result = await self.forecast_coordinator.ensure_forecast(
            ctx.participant_id,
            datetime.now(self.timezone).date(),
            "user_curve_request",
            refresh_calendar=True,
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
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        if self.pressure_curves is None:
            raise RuntimeError("pressure curve service is unavailable")
        try:
            target = (
                date.fromisoformat(str(args["local_date"]))
                if args.get("local_date")
                else datetime.now(self.timezone).date()
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("local_date must be YYYY-MM-DD") from exc
        try:
            view = await self.pressure_curves.build(
                ctx.participant_id,
                target,
                reason="user_curve_card_request",
                refresh_calendar=True,
                stress_only=True,
            )
        except HistoricalForecastNotFoundError:
            return {
                "ok": False,
                "error": "historical_forecast_not_found",
                "local_date": target.isoformat(),
            }
        result = view.forecast
        analysis = view.analysis
        card = pressure_curve_card(
            analysis,
            image_key=IMAGE_KEY_PLACEHOLDER,
            local_date=str(result.get("local_date") or datetime.now(self.timezone).date()),
            model_output=dict(result.get("output") or {}),
            requested_date_is_today=(target == datetime.now(self.timezone).date()),
        )
        if self.presentations is None:
            raise RuntimeError("rich reply delivery is unavailable")
        self.presentations.stage_image_card(ctx.agent_run_id, view.png_bytes, card)
        return {
            "ok": True,
            "card_queued": True,
            "local_date": str(result.get("local_date") or ""),
            "point_count": analysis.point_count,
            "predicted_peak": {
                "time": analysis.peak_stress_time,
                "stress_0_10": analysis.peak_stress,
            },
            "curve_analysis": analysis.to_dict(),
            "calendar_degraded": bool(result.get("calendar_degraded")),
        }

    async def _refresh_calendar_mutation_forecasts(
        self,
        participant_id: Any,
        dates: set[Any],
        reason: str,
    ) -> dict[str, Any]:
        if self.forecast_coordinator is None:
            return {
                "forecast_refresh": "not_configured",
                "forecast_refresh_degraded": False,
                "forecast_refreshed_dates": [],
                "forecast_refresh_errors": [],
            }
        today = datetime.now(self.timezone).date()
        supplied = sorted(value for value in dates if value is not None)
        normalized = [value for value in supplied if value >= today]
        if not supplied:
            normalized = [today]
        elif not normalized:
            return {
                "forecast_refresh": "historical_dates_skipped",
                "forecast_refresh_degraded": False,
                "forecast_refreshed_dates": [],
                "forecast_refresh_errors": [],
            }
        dependency = {}
        if today in normalized:
            tomorrow = today + timedelta(days=1)
            if tomorrow not in normalized:
                normalized.append(tomorrow)
                normalized.sort()
            dependency[tomorrow.isoformat()] = "previous_day_terminal_changed"

        # Preserve dependency order for today -> tomorrow. Other independent
        # dates remain deterministic and are inexpensive at mutation time.
        results = []
        for target in normalized:
            try:
                result = await self.forecast_coordinator.ensure_forecast(
                    participant_id,
                    target,
                    reason,
                    refresh_calendar=True,
                )
            except BaseException as exc:
                result = exc
            results.append(result)
        refreshed = [
            target.isoformat() for target, result in zip(normalized, results)
            if not isinstance(result, BaseException)
        ]
        errors = [
            {"local_date": target.isoformat(), "error_class": type(result).__name__}
            for target, result in zip(normalized, results)
            if isinstance(result, BaseException)
        ]
        return {
            "forecast_refresh": (
                "succeeded" if not errors else "failed" if not refreshed else "partial"
            ),
            "forecast_refresh_degraded": bool(errors),
            "forecast_refreshed_dates": refreshed,
            "forecast_refresh_errors": errors,
            "forecast_dependency_refresh": dependency,
        }

    def _event_dates(self, event: dict[str, Any] | None) -> set[Any]:
        if not event:
            return set()
        result = set()
        for key in ("start_time", "end_time"):
            value = event.get(key)
            if value:
                try:
                    result.add(_parse_datetime(value, self.timezone).date())
                except ValueError:
                    continue
        return result

    def get_checkin_card(
        self, ctx: AgentContext, _args: dict[str, Any]
    ) -> dict[str, Any]:
        if self.presentations is None:
            raise RuntimeError("rich reply delivery is unavailable")
        self.presentations.stage_card(ctx.agent_run_id, daily_checkin_card())
        return {
            "ok": True,
            "card_queued": True,
            "questionnaire": "daily_non_clinical_checkin_v1",
        }

    def calendar_connection_status(
        self, ctx: AgentContext, _args: dict[str, Any]
    ) -> dict[str, Any]:
        status = self.tokens.status(ctx.participant_id)
        scopes = set(status.get("scopes") or [])
        create_enabled = bool(
            "calendar:calendar.event:create" in scopes or "calendar:calendar" in scopes
        )
        return {
            "ok": True,
            **status,
            "calendar_write_enabled": create_enabled,
            "calendar_create_enabled": create_enabled,
            "calendar_update_enabled": bool(
                "calendar:calendar.event:update" in scopes or "calendar:calendar" in scopes
            ),
            "calendar_delete_enabled": bool(
                "calendar:calendar.event:delete" in scopes or "calendar:calendar" in scopes
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
        recurrence = _recurrence_from_args(args, self.timezone)
        try:
            event = await self.calendar.create_event(
                ctx.participant_id,
                summary=str(args["summary"]),
                description=str(args.get("description") or ""),
                start_time=start_time,
                end_time=end_time,
                reminder_minutes=args.get("reminder_minutes"),
                recurrence=recurrence,
                source_message_id=ctx.message_id,
            )
        except PermissionError:
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        refresh = await self._refresh_calendar_mutation_forecasts(
            ctx.participant_id,
            self._event_dates(event) or {start_time.date(), end_time.date()},
            "calendar_create_event",
        )
        return {"ok": True, "calendar_mutation": "succeeded", "created": event, **refresh}

    async def update_calendar_event(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        start_time = (
            _parse_datetime(args["start_time"], self.timezone)
            if args.get("start_time") is not None
            else None
        )
        end_time = (
            _parse_datetime(args["end_time"], self.timezone)
            if args.get("end_time") is not None
            else None
        )
        recurrence = _recurrence_from_args(args, self.timezone)
        try:
            previous = await self.calendar.get_event(
                ctx.participant_id, str(args["event_id"])
            )
            event = await self.calendar.update_event(
                ctx.participant_id,
                str(args["event_id"]),
                summary=args.get("summary"),
                description=args.get("description"),
                start_time=start_time,
                end_time=end_time,
                reminder_minutes=args.get("reminder_minutes"),
                recurrence=recurrence,
                clear_recurrence=bool(args.get("clear_recurrence", False)),
            )
        except PermissionError:
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        dates = self._event_dates(previous) | self._event_dates(event)
        if start_time is not None:
            dates.update({start_time.date(), end_time.date()})
        refresh = await self._refresh_calendar_mutation_forecasts(
            ctx.participant_id, dates, "calendar_update_event"
        )
        return {"ok": True, "calendar_mutation": "succeeded", "updated": event, **refresh}

    async def delete_calendar_event(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        if args.get("confirmed") is not True:
            return {"ok": False, "error": "explicit_confirmation_required"}
        try:
            previous = await self.calendar.get_event(
                ctx.participant_id, str(args["event_id"])
            )
            deleted = await self.calendar.delete_event(
                ctx.participant_id, str(args["event_id"])
            )
        except PermissionError:
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        refresh = await self._refresh_calendar_mutation_forecasts(
            ctx.participant_id,
            self._event_dates(previous),
            "calendar_delete_event",
        )
        return {"ok": True, "calendar_mutation": "succeeded", "deleted": deleted, **refresh}
