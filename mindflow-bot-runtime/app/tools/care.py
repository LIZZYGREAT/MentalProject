"""Production allowlist of participant-bound care and calendar tools."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any
import uuid
from zoneinfo import ZoneInfo


from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.integrations.feishu.cards import daily_checkin_card, pressure_curve_card
from app.integrations.feishu.calendar import (
    CalendarMutationOutcomeUnknown,
    CalendarMutationRejected,
    CalendarService,
    build_recurrence_rule,
)
from app.repositories import (
    ObservationRepository,
    ProfileRepository,
    ForecastSnapshotRepository,
    LearnedProfileRepository,
)
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.care_message_service import CareMessageService
from app.services.care_context import CARE_RECENT_OBSERVATION_MAX_AGE_MINUTES
from app.services.observation_forecast_refresh import ObservationForecastRefreshService
from app.services.calendar_mutation_impact import CalendarMutationImpactResolver
from app.services.forecast_mutation_refresh import ForecastMutationRefreshQueue
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


def _public_care_preferences(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    reserved = {"morning_brief_enabled", "weekly_summary_enabled"}
    return {key: item for key, item in value.items() if key not in reserved}


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
    recurrence = build_recurrence_rule(
        str(frequency),
        interval=int(args.get("recurrence_interval", 1)),
        weekdays=list(args.get("recurrence_weekdays") or []),
        count=args.get("recurrence_count"),
        until=until,
    )
    if args.get("start_time") is not None:
        _validate_generated_weekly_recurrence(
            recurrence,
            _parse_datetime(args["start_time"], timezone_value),
        )
    return recurrence


def _validate_generated_weekly_recurrence(
    recurrence: str | None, start_time: datetime
) -> None:
    if not recurrence:
        return
    parts = {}
    for item in recurrence.split(";"):
        name, separator, value = item.partition("=")
        if separator:
            parts[name.upper()] = value.upper()
    if parts.get("FREQ") != "WEEKLY" or not parts.get("BYDAY"):
        return
    weekday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[
        start_time.weekday()
    ]
    if weekday not in {item.strip() for item in parts["BYDAY"].split(",")}:
        raise ValueError("weekly recurrence must include the local start weekday")


class CareTools:
    def __init__(
        self,
        profiles: ProfileRepository,
        observations: ObservationRepository,
        calendar: CalendarService,
        tokens: TokenRepository,
        timezone_name: str,
        forecast_coordinator: ForecastCoordinator,
        forecast_snapshots: ForecastSnapshotRepository | None = None,
        presentations: PresentationOutbox | None = None,
        learned_profiles: LearnedProfileRepository | None = None,
        pressure_curves: PressureCurveService | None = None,
        observation_refresh: ObservationForecastRefreshService | None = None,
        mutation_refresh: ForecastMutationRefreshQueue | None = None,
        care_preferences: Any = None,
        care_interventions: Any = None,
    ):
        self.profiles = profiles
        self.observations = observations
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
        self.mutation_refresh = mutation_refresh
        self.care_messages = (
            getattr(forecast_coordinator, "care_messages", None)
            or CareMessageService(timezone_name)
        )
        self.care_preferences = care_preferences
        self.care_interventions = care_interventions
        self.calendar_mutation_impact = CalendarMutationImpactResolver(
            timezone_name
        )

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "care_get_today_context",
            "Return this participant's current profile, recent check-in, and latest forecast.",
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
            "Return optional support using the same reviewed calendar, forecast, and recent-state care policy as proactive warnings.",
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
            "care_update_preferences",
            "Update this participant's durable care, warning, review, quiet-hour, and follow-up preferences without exceeding backend safety limits.",
            {
                "type": "object",
                "properties": {
                    "care_enabled": {"type": "boolean"},
                    "warning_enabled": {"type": "boolean"},
                    "daily_review_enabled": {"type": "boolean"},
                    "quiet_hours_start": {
                        "type": "string",
                        "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                    },
                    "quiet_hours_end": {
                        "type": "string",
                        "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                    },
                    "clear_quiet_hours": {"type": "boolean", "const": True},
                    "max_proactive_care_per_day": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "allow_schedule_suggestions": {"type": "boolean"},
                    "allow_follow_up": {"type": "boolean"},
                    "preferred_support_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "micro_break",
                                "hydration",
                                "walk",
                                "task_decomposition",
                                "transition_buffer",
                                "recovery",
                                "trusted_person",
                            ],
                        },
                        "uniqueItems": True,
                        "maxItems": 7,
                    },
                },
                "minProperties": 1,
                "additionalProperties": False,
            },
            self.update_care_preferences,
        )
        registry.register(
            "care_respond_to_latest_intervention",
            "Record one explicit action or quick feedback for this participant's latest delivered care intervention.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "ack",
                            "snooze_30",
                            "mute_today",
                            "helpful",
                            "not_relevant",
                            "too_early",
                            "too_late",
                        ],
                    },
                    "comment": {"type": "string", "maxLength": 500},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            self.respond_to_latest_care,
        )
        registry.register(
            "care_get_pressure_curve",
            "Generate a participant-bound pressure forecast for today or a requested future local date and queue its pressure-only curve card. A past date is read-only and succeeds only when its original forecast was persisted.",
            {
                "type": "object",
                "properties": {
                    "local_date": {
                        "type": "string",
                        "format": "date",
                        "description": "Preserve the user's requested local calendar date in YYYY-MM-DD. Omit only when the user requests today; future dates are supported, while past dates only read a persisted original forecast.",
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
                "dependentRequired": {
                    "start_time": ["end_time"],
                    "end_time": ["start_time"],
                },
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
        recent = self.observations.recent_before(
            ctx.participant_id,
            before=datetime.now(timezone.utc),
            max_age=timedelta(
                minutes=CARE_RECENT_OBSERVATION_MAX_AGE_MINUTES
            ),
            limit=1,
        )
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
            "latest_forecast": latest_forecast,
            "care_preferences": (
                _public_care_preferences(
                    self.care_preferences.get(ctx.participant_id)
                )
                if self.care_preferences is not None else None
            ),
            "latest_care_intervention": (
                self.care_interventions.latest_sent(ctx.participant_id)
                if self.care_interventions is not None else None
            ),
        }

    def record_checkin(self, ctx: AgentContext, args: dict[str, Any]) -> dict[str, Any]:
        if self.observation_refresh is None:
            raise RuntimeError("observation forecast refresh service is unavailable")
        payload = {
            "stress_0_10": float(args["stress"]),
            "energy_0_10": float(args["energy"]),
            "activity": str(args["activity"]),
            "stress_event_since_last": bool(args["stress_event_since_last"]),
            "event_ongoing": bool(args["event_ongoing"]),
        }
        write = self.observations.add_with_status(
            ctx.participant_id,
            "checkin",
            payload,
            source_message_id=ctx.message_id,
        )
        self.observation_refresh.on_observation_committed(
            participant_id=ctx.participant_id,
            observed_at=write.observed_at,
            created=write.created,
        )
        if write.idempotency_conflict:
            return {
                "ok": False,
                "error": "idempotency_conflict",
                "observation_id": str(write.observation_id),
                "recorded": dict(write.persisted_payload),
            }
        persisted = write.persisted_payload
        return {
            "ok": True,
            "observation_id": str(write.observation_id),
            "created": write.created,
            "recorded": {
                "stress": persisted.get("stress_0_10"),
                "energy": persisted.get("energy_0_10"),
            },
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

    async def get_support(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        if self.forecast_coordinator is None:
            raise RuntimeError("forecast care context is unavailable")
        target = datetime.now(self.timezone).date()
        forecast = await self.forecast_coordinator.ensure_forecast(
            ctx.participant_id,
            target,
            "user_requested_support",
            refresh_calendar=True,
        )
        before = datetime.now(timezone.utc)
        preference_read = (
            asyncio.to_thread(self.care_preferences.get, ctx.participant_id)
            if self.care_preferences is not None
            else asyncio.sleep(0, result=None)
        )
        recent, profile_row, care_preferences = await asyncio.gather(
            asyncio.to_thread(
                self.observations.recent_before,
                ctx.participant_id,
                before=before,
                max_age=timedelta(
                    minutes=CARE_RECENT_OBSERVATION_MAX_AGE_MINUTES
                ),
                limit=1,
            ),
            asyncio.to_thread(self.profiles.current, ctx.participant_id),
            preference_read,
        )
        observation = recent[0] if recent else None
        output = dict(forecast.get("output") or {})
        raw_alerts = [
            item for item in (output.get("alerts") or []) if isinstance(item, dict)
        ]
        alert = self._support_alert(raw_alerts, observation)
        contextual = self.care_messages.contextualize_alert(
            alert,
            source="user_requested_support",
            local_date=target,
            calendar_events=list(forecast.get("calendar_events") or []),
            calendar_degraded=bool(forecast.get("calendar_degraded")),
            recent_observation=observation,
            profile=(profile_row or {}).get("profile"),
            profile_version=(profile_row or {}).get("version"),
            care_preferences=care_preferences,
        )
        provenance = dict(contextual["care_provenance"])
        provenance.update(
            {
                "source_forecast_id": forecast.get("id"),
                "forecast_version": forecast.get("forecast_version"),
            }
        )
        return {
            "ok": True,
            "support_type": contextual["care_plan"]["intervention_type"],
            "suggestion": contextual["message"],
            "care_plan": contextual["care_plan"],
            "care_context": contextual["care_context"],
            "care_provenance": provenance,
            "context_acknowledged": bool(args.get("context")),
        }

    def update_care_preferences(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        if self.care_preferences is None:
            raise RuntimeError("care preference service is unavailable")
        changes = dict(args)
        if changes.pop("clear_quiet_hours", False):
            changes["quiet_hours_start"] = None
            changes["quiet_hours_end"] = None
        preferences = self.care_preferences.update(ctx.participant_id, changes)
        return {
            "ok": True,
            "care_preferences": _public_care_preferences(preferences),
        }

    def respond_to_latest_care(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        if self.care_interventions is None:
            raise RuntimeError("care intervention service is unavailable")
        latest = self.care_interventions.latest_sent(ctx.participant_id)
        if latest is None:
            return {"ok": False, "error": "no_delivered_care_intervention"}
        action = str(args["action"])
        result = self.care_interventions.apply_action(
            ctx.participant_id,
            uuid.UUID(latest["id"]),
            action=action,
            callback_event_id=f"agent:{ctx.message_id}:{action}"[:160],
            optional_comment=args.get("comment"),
        )
        return {
            "ok": True,
            "created": result["created"],
            "action_result": result["action_result"],
            "care_intervention": result["intervention"],
        }

    def _support_alert(
        self,
        alerts: list[dict[str, Any]],
        observation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = datetime.now(self.timezone)

        def alert_minute(item: dict[str, Any]) -> int:
            try:
                hour, minute = (
                    int(part)
                    for part in str(item.get("time") or "23:59")[:5].split(":")
                )
                return hour * 60 + minute
            except (TypeError, ValueError):
                return 1439

        now_minute = now.hour * 60 + now.minute
        future = [item for item in alerts if alert_minute(item) >= now_minute]
        if future:
            return min(future, key=alert_minute)

        payload = dict((observation or {}).get("payload") or {})
        try:
            stress = float(payload.get("stress_0_10", 0.0))
        except (TypeError, ValueError):
            stress = 0.0
        try:
            energy = float(payload.get("energy_0_10", 10.0))
        except (TypeError, ValueError):
            energy = 10.0
        return {
            "time": now.strftime("%H:%M"),
            "tier": 2 if stress >= 7.0 else 1,
            "S": stress,
            "V": energy,
            "F": 0.0,
            "trigger_source": "user_requested_support",
            "care_action": (
                "protected_break"
                if stress >= 7.0 or energy <= 3.5
                else "brief_check_in"
            ),
            "current_events": [],
            "dominant_stressors": [],
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
        *,
        reconciliation_id: str | None = None,
    ) -> dict[str, Any]:
        if self.forecast_coordinator is None:
            return {
                "forecast_refresh": "not_configured",
                "forecast_refresh_degraded": False,
                "forecast_refreshed_dates": [],
                "forecast_refresh_errors": [],
            }
        today, direct_dates, refresh_targets, dependency_sources = (
            self._calendar_mutation_work(dates)
        )
        if not refresh_targets:
            return {
                "forecast_refresh": "historical_dates_skipped",
                "forecast_refresh_degraded": False,
                "forecast_refreshed_dates": [],
                "forecast_refresh_errors": [],
            }
        normalized = sorted(refresh_targets)
        dependency_refresh = getattr(
            self.forecast_coordinator, "dependency_refresh", None
        )
        dependency = {
            target.isoformat(): "previous_day_terminal_changed"
            for target in dependency_sources
        }
        mutation_refresh = getattr(self, "mutation_refresh", None)
        reconciliations = getattr(mutation_refresh, "reconciliations", None)
        reconciliation = (
            await asyncio.to_thread(reconciliations.get, reconciliation_id)
            if reconciliations is not None and reconciliation_id is not None
            else None
        )
        # The remote provider mutation has committed by the time this method is
        # called. Persist the complete local work set before attempting any
        # invalidation so a process crash cannot consume the mutation.
        if reconciliations is not None and reconciliation is None:
            reconciliation = await asyncio.to_thread(
                reconciliations.create,
                participant_id,
                mutation_kind=reason,
                direct_dates=direct_dates,
                refresh_targets=refresh_targets,
                dependency_sources=dependency_sources,
            )

        processing_claim_token: uuid.UUID | None = None
        if reconciliation is not None:
            processing_claim_token = uuid.uuid4()
            claimed = await asyncio.to_thread(
                reconciliations.claim_processing,
                reconciliation["id"],
                claim_token=processing_claim_token,
            )
            if claimed is None:
                return {
                    "forecast_refresh": "recovery_in_progress",
                    "forecast_refresh_degraded": False,
                    "forecast_refreshed_dates": [],
                    "forecast_refresh_errors": [],
                    "calendar_mutation_reconciliation_id": reconciliation["id"],
                }
            reconciliation = claimed

        forecast_repository = (
            getattr(self, "forecast_snapshots", None)
            or getattr(self.forecast_coordinator, "forecasts", None)
        )
        invalidation_errors: list[tuple[Any, Exception]] = []
        failed_dependency_sources: dict[date, date] = {}
        if forecast_repository is not None:
            # Fail closed for every affected date before starting any expensive
            # recomputation. In particular, tomorrow must stop exposing a stale
            # Today terminal while today's refresh is still in flight.
            batch_invalidate = getattr(
                forecast_repository,
                "invalidate_for_calendar_mutation_dates",
                None,
            )
            if direct_dates and callable(batch_invalidate):
                try:
                    await asyncio.to_thread(
                        batch_invalidate,
                        self.forecast_coordinator.warnings,
                        participant_id,
                        direct_dates,
                        reason=reason,
                    )
                except asyncio.CancelledError:
                    if processing_claim_token is not None:
                        await asyncio.to_thread(
                            reconciliations.release_processing,
                            reconciliation["id"],
                            claim_token=processing_claim_token,
                        )
                    raise
                except Exception as exc:
                    invalidation_errors.extend(
                        (target, exc) for target in sorted(direct_dates)
                    )
            elif direct_dates:
                for target in sorted(direct_dates):
                    try:
                        await asyncio.to_thread(
                            forecast_repository.invalidate_for_calendar_mutation,
                            self.forecast_coordinator.warnings,
                            participant_id,
                            target,
                            reason=reason,
                        )
                    except asyncio.CancelledError:
                        if processing_claim_token is not None:
                            await asyncio.to_thread(
                                reconciliations.release_processing,
                                reconciliation["id"],
                                claim_token=processing_claim_token,
                            )
                        raise
                    except Exception as exc:
                        invalidation_errors.append((target, exc))
            for target in sorted(set(normalized) - direct_dates):
                try:
                    if dependency_refresh is not None:
                        await asyncio.to_thread(
                            dependency_refresh.invalidate_dependent_now,
                            participant_id,
                            today,
                            reason="previous_day_terminal_changed",
                        )
                    else:
                        await asyncio.to_thread(
                            self.forecast_coordinator.mark_dependency_dirty,
                            participant_id,
                            target,
                            reason="previous_day_terminal_changed",
                        )
                except asyncio.CancelledError:
                    if processing_claim_token is not None:
                        await asyncio.to_thread(
                            reconciliations.release_processing,
                            reconciliation["id"],
                            claim_token=processing_claim_token,
                        )
                    raise
                except Exception as exc:
                    invalidation_errors.append((target, exc))
                    failed_dependency_sources[target] = today

        failed_dates = {target for target, _error in invalidation_errors}
        queued = False
        if mutation_refresh is not None:
            enqueue_kwargs: dict[str, Any] = {"reason": reason}
            failed_direct_dates = failed_dates & direct_dates
            if failed_direct_dates:
                enqueue_kwargs["invalidation_dates"] = failed_direct_dates
            if failed_dependency_sources:
                enqueue_kwargs["dependency_invalidation_sources"] = (
                    failed_dependency_sources
                )
            if reconciliation is not None:
                enqueue_kwargs["reconciliation_id"] = reconciliation["id"]
            queued = bool(
                mutation_refresh.enqueue(
                    participant_id,
                    refresh_targets,
                    **enqueue_kwargs,
                )
            )
        if reconciliation is not None and not invalidation_errors:
            try:
                await asyncio.to_thread(
                    reconciliations.mark_fenced, reconciliation["id"]
                )
            except asyncio.CancelledError:
                if processing_claim_token is not None:
                    await asyncio.to_thread(
                        reconciliations.release_processing,
                        reconciliation["id"],
                        claim_token=processing_claim_token,
                    )
                raise
        if reconciliation is not None and not queued and processing_claim_token is not None:
            await asyncio.to_thread(
                reconciliations.release_processing,
                reconciliation["id"],
                claim_token=processing_claim_token,
            )
        errors = [
            {"local_date": target.isoformat(), "error_class": type(error).__name__}
            for target, error in invalidation_errors
        ]
        return {
            "forecast_refresh": (
                "partial" if errors and queued
                else "failed" if errors
                else "queued" if queued
                else "deferred"
            ),
            "forecast_refresh_degraded": bool(errors),
            "forecast_invalidation": "failed" if errors else "succeeded",
            "forecast_invalidation_retry": bool(errors and queued),
            "forecast_refreshed_dates": [],
            "forecast_refresh_queued_dates": (
                [target.isoformat() for target in sorted(refresh_targets)]
                if queued
                else []
            ),
            "forecast_refresh_errors": errors,
            "forecast_dependency_refresh": dependency,
            "calendar_mutation_reconciliation_id": (
                reconciliation["id"] if reconciliation is not None else None
            ),
        }

    def _calendar_mutation_work(
        self, dates: set[Any]
    ) -> tuple[date, set[date], dict[date, bool], dict[date, date]]:
        today = datetime.now(self.timezone).date()
        supplied = sorted(value for value in dates if value is not None)
        normalized = [value for value in supplied if value >= today]
        if not supplied:
            normalized = [today]
        elif not normalized:
            return today, set(), {}, {}
        direct_dates = set(normalized)
        dependency_refresh = getattr(
            self.forecast_coordinator, "dependency_refresh", None
        )
        if today in direct_dates:
            tomorrow = (
                dependency_refresh.dependent_date(today)
                if dependency_refresh is not None
                else today + timedelta(days=1)
            )
            if tomorrow is None:
                tomorrow = today + timedelta(days=1)
            if tomorrow not in direct_dates:
                normalized.append(tomorrow)
        refresh_targets = {
            target: target in direct_dates for target in sorted(set(normalized))
        }
        dependency_sources = {
            target: today for target in refresh_targets if target not in direct_dates
        }
        return today, direct_dates, refresh_targets, dependency_sources

    async def _prepare_calendar_mutation_reconciliation(
        self,
        participant_id: Any,
        dates: set[Any],
        reason: str,
        operation: dict[str, Any],
    ) -> dict[str, Any] | None:
        mutation_refresh = getattr(self, "mutation_refresh", None)
        reconciliations = getattr(mutation_refresh, "reconciliations", None)
        if reconciliations is None:
            return None
        _today, direct_dates, refresh_targets, dependency_sources = (
            self._calendar_mutation_work(dates)
        )
        return await asyncio.to_thread(
            reconciliations.create,
            participant_id,
            mutation_kind=reason,
            direct_dates=direct_dates,
            refresh_targets=refresh_targets,
            dependency_sources=dependency_sources,
            operation=operation,
        )

    async def _finish_remote_mutation_intent(
        self,
        reconciliation: dict[str, Any] | None,
        *,
        provider_result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        if reconciliation is None:
            return
        reconciliations = self.mutation_refresh.reconciliations
        if error is None:
            await asyncio.to_thread(
                reconciliations.mark_remote_committed,
                reconciliation["id"],
                provider_result=provider_result or {},
            )
        else:
            outcome_unknown = isinstance(error, CalendarMutationOutcomeUnknown)
            method = (
                reconciliations.mark_remote_outcome_unknown
                if outcome_unknown
                else reconciliations.mark_remote_failed
            )
            await asyncio.to_thread(
                method, reconciliation["id"], error_class=type(error).__name__
            )

    @staticmethod
    def _calendar_outcome_unknown(error: BaseException) -> bool:
        return isinstance(
            error,
            CalendarMutationOutcomeUnknown,
        )

    async def _calendar_mutation_dates(
        self,
        participant_id: Any,
        *,
        previous: dict[str, Any] | None,
        updated: dict[str, Any] | None,
        updated_recurrence: str | None = None,
        clear_recurrence: bool = False,
    ) -> set[date]:
        forecast_repository = (
            getattr(self, "forecast_snapshots", None)
            or getattr(self.forecast_coordinator, "forecasts", None)
        )
        valid_dates = getattr(forecast_repository, "valid_dates", None)
        persisted_dates = (
            await asyncio.to_thread(
                valid_dates, participant_id
            )
            if callable(valid_dates)
            else set()
        )
        return self.calendar_mutation_impact.affected_dates(
            previous=previous,
            updated=updated,
            persisted_dates=persisted_dates,
            updated_recurrence=updated_recurrence,
            clear_recurrence=clear_recurrence,
        )

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
        requested_event = {
            "summary": str(args["summary"]),
            "description": str(args.get("description") or ""),
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "recurrence": recurrence or "",
            "reminder_minutes": args.get("reminder_minutes"),
        }
        dates = await self._calendar_mutation_dates(
            ctx.participant_id,
            previous=None,
            updated=requested_event,
            updated_recurrence=recurrence,
        )
        reconciliation = await self._prepare_calendar_mutation_reconciliation(
            ctx.participant_id,
            dates,
            "calendar_create_event",
            {
                "operation_type": "create",
                "source_message_id": ctx.message_id,
                "requested": requested_event,
            },
        )
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
        except PermissionError as exc:
            await self._finish_remote_mutation_intent(
                reconciliation, error=exc
            )
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        except Exception as exc:
            await self._finish_remote_mutation_intent(
                reconciliation, error=exc
            )
            if self._calendar_outcome_unknown(exc):
                await self._refresh_calendar_mutation_forecasts(
                    ctx.participant_id,
                    dates,
                    "calendar_create_event_outcome_unknown",
                    reconciliation_id=(
                        reconciliation["id"] if reconciliation is not None else None
                    ),
                )
            raise
        await self._finish_remote_mutation_intent(
            reconciliation, provider_result=dict(event or {})
        )
        refresh = await self._refresh_calendar_mutation_forecasts(
            ctx.participant_id,
            dates,
            "calendar_create_event",
            reconciliation_id=(
                reconciliation["id"] if reconciliation is not None else None
            ),
        )
        return {"ok": True, "calendar_mutation": "succeeded", "created": event, **refresh}

    async def update_calendar_event(
        self, ctx: AgentContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        if ("start_time" in args) != ("end_time" in args):
            raise ValueError("start_time and end_time must be provided together")
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
        except PermissionError:
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        previous_event = dict(previous or {})
        clear_recurrence = bool(args.get("clear_recurrence", False))
        requested_event = {
            **previous_event,
            **({"summary": args["summary"]} if args.get("summary") is not None else {}),
            **(
                {"description": args["description"]}
                if args.get("description") is not None
                else {}
            ),
            "start_time": (
                start_time.isoformat()
                if start_time is not None
                else previous_event.get("start_time")
            ),
            "end_time": (
                end_time.isoformat()
                if end_time is not None
                else previous_event.get("end_time")
            ),
            "recurrence": (
                "" if clear_recurrence
                else recurrence
                if recurrence is not None
                else previous_event.get("recurrence") or ""
            ),
            **(
                {"reminder_minutes": args.get("reminder_minutes")}
                if "reminder_minutes" in args
                else {}
            ),
        }
        if recurrence is not None and start_time is None:
            previous_start = requested_event.get("start_time")
            if previous_start:
                _validate_generated_weekly_recurrence(
                    recurrence,
                    _parse_datetime(previous_start, self.timezone),
                )
        dates = await self._calendar_mutation_dates(
            ctx.participant_id,
            previous=previous_event,
            updated=requested_event,
            updated_recurrence=recurrence,
            clear_recurrence=clear_recurrence,
        )
        reconciliation = await self._prepare_calendar_mutation_reconciliation(
            ctx.participant_id,
            dates,
            "calendar_update_event",
            {
                "operation_type": "update",
                "event_id": str(args["event_id"]),
                "previous": previous_event,
                "requested": requested_event,
            },
        )
        try:
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
        except PermissionError as exc:
            await self._finish_remote_mutation_intent(
                reconciliation, error=exc
            )
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        except Exception as exc:
            await self._finish_remote_mutation_intent(
                reconciliation, error=exc
            )
            if self._calendar_outcome_unknown(exc):
                await self._refresh_calendar_mutation_forecasts(
                    ctx.participant_id,
                    dates,
                    "calendar_update_event_outcome_unknown",
                    reconciliation_id=(
                        reconciliation["id"] if reconciliation is not None else None
                    ),
                )
            raise
        await self._finish_remote_mutation_intent(
            reconciliation, provider_result=dict(event or {})
        )
        refresh = await self._refresh_calendar_mutation_forecasts(
            ctx.participant_id,
            dates,
            "calendar_update_event",
            reconciliation_id=(
                reconciliation["id"] if reconciliation is not None else None
            ),
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
        except PermissionError:
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        dates = await self._calendar_mutation_dates(
            ctx.participant_id,
            previous=previous,
            updated=None,
        )
        reconciliation = await self._prepare_calendar_mutation_reconciliation(
            ctx.participant_id,
            dates,
            "calendar_delete_event",
            {
                "operation_type": "delete",
                "event_id": str(args["event_id"]),
                "previous": dict(previous or {}),
            },
        )
        try:
            deleted = await self.calendar.delete_event(
                ctx.participant_id, str(args["event_id"])
            )
        except PermissionError as exc:
            await self._finish_remote_mutation_intent(
                reconciliation, error=exc
            )
            return {"ok": False, "error": "calendar_not_connected", "command": "/calendar"}
        except Exception as exc:
            await self._finish_remote_mutation_intent(
                reconciliation, error=exc
            )
            if self._calendar_outcome_unknown(exc):
                await self._refresh_calendar_mutation_forecasts(
                    ctx.participant_id,
                    dates,
                    "calendar_delete_event_outcome_unknown",
                    reconciliation_id=(
                        reconciliation["id"] if reconciliation is not None else None
                    ),
                )
            raise
        await self._finish_remote_mutation_intent(
            reconciliation,
            provider_result={"deleted": bool(deleted), "event_id": str(args["event_id"])},
        )
        refresh = await self._refresh_calendar_mutation_forecasts(
            ctx.participant_id,
            dates,
            "calendar_delete_event",
            reconciliation_id=(
                reconciliation["id"] if reconciliation is not None else None
            ),
        )
        return {"ok": True, "calendar_mutation": "succeeded", "deleted": deleted, **refresh}
