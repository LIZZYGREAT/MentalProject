"""Explicit, secret-free read models for the administrator API."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
import uuid

from sqlalchemy import desc, func, or_, select

from app.db import Database
from app.models import (
    AgentRun,
    AgentToolCall,
    BotEvent,
    CalendarSnapshot,
    CareInterventionEvent,
    CareInterventionFeedback,
    FeishuBinding,
    FeishuOAuthToken,
    ForecastSnapshot,
    LearnedModelProfile,
    Participant,
    ParticipantCarePreference,
    ParticipantProfile,
    RuntimeIncident,
    StateObservation,
    WarningSchedule,
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (date, datetime)) else None


_SECRET_KEY_PARTS = (
    "ciphertext",
    "access_token",
    "refresh_token",
    "device_code",
    "password",
    "secret",
    "api_key",
    "encryption_key",
    "student_no",
)


def _redact(value: Any, depth: int = 0) -> Any:
    """Enforce response-time defense in depth for nested persisted JSON."""

    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): _redact(child, depth + 1)
            for key, child in value.items()
            if not any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value[:1000]]
    return value


class AdminRepository:
    def __init__(self, database: Database):
        self.database = database

    def health(self) -> dict[str, Any]:
        with self.database.session() as session:
            session.execute(select(1)).scalar_one()
        return {"status": "ok", "database": "ok"}

    def dashboard(self) -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        with self.database.session() as session:
            return {
                "active_participants": session.scalar(
                    select(func.count()).select_from(Participant).where(
                        Participant.status == "active"
                    )
                ) or 0,
                "messages_24h": session.scalar(
                    select(func.count()).select_from(BotEvent).where(
                        BotEvent.received_at >= since
                    )
                ) or 0,
                "completed_24h": session.scalar(
                    select(func.count()).select_from(BotEvent).where(
                        BotEvent.received_at >= since,
                        BotEvent.status == "completed",
                    )
                ) or 0,
                "failed_24h": session.scalar(
                    select(func.count()).select_from(BotEvent).where(
                        BotEvent.received_at >= since,
                        BotEvent.status.in_(("failed", "failed_replied", "reply_pending")),
                    )
                ) or 0,
                "pending_warnings": session.scalar(
                    select(func.count()).select_from(WarningSchedule).where(
                        WarningSchedule.status == "pending"
                    )
                ) or 0,
                "sent_warnings": session.scalar(
                    select(func.count()).select_from(WarningSchedule).where(
                        WarningSchedule.status == "sent"
                    )
                ) or 0,
                "calendar_degraded": session.scalar(
                    select(func.count()).select_from(CalendarSnapshot).where(
                        CalendarSnapshot.degraded.is_(True)
                    )
                ) or 0,
                "runtime_incidents": session.scalar(
                    select(func.count()).select_from(RuntimeIncident).where(
                        RuntimeIncident.created_at >= since
                    )
                ) or 0,
            }

    def participants(
        self,
        *,
        search: str = "",
        status: str = "",
        page: int = 1,
        limit: int = 25,
    ) -> dict[str, Any]:
        page = max(1, page)
        limit = max(1, min(limit, 100))
        with self.database.session() as session:
            conditions = []
            if search:
                conditions.append(Participant.participant_code.ilike(f"%{search[:32]}%"))
            if status:
                conditions.append(Participant.status == status[:32])
            total = session.scalar(
                select(func.count()).select_from(Participant).where(*conditions)
            ) or 0
            rows = session.execute(
                select(Participant)
                .where(*conditions)
                .order_by(Participant.participant_code)
                .offset((page - 1) * limit)
                .limit(limit)
            ).scalars().all()
            items = [self._participant_row(session, row) for row in rows]
        return {"items": items, "page": page, "limit": limit, "total": total}

    def participant(self, participant_code: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.execute(
                select(Participant).where(
                    Participant.participant_code == participant_code
                )
            ).scalar_one_or_none()
            return self._participant_detail(session, row) if row else None

    def participant_id(self, participant_code: str) -> uuid.UUID | None:
        with self.database.session() as session:
            return session.execute(
                select(Participant.id).where(
                    Participant.participant_code == participant_code
                )
            ).scalar_one_or_none()

    @staticmethod
    def _participant_row(session, row: Participant) -> dict[str, Any]:
        binding = session.execute(
            select(FeishuBinding).where(FeishuBinding.participant_id == row.id)
        ).scalar_one_or_none()
        token = session.get(FeishuOAuthToken, row.id)
        profile_version = session.scalar(
            select(func.max(ParticipantProfile.version)).where(
                ParticipantProfile.participant_id == row.id
            )
        )
        learned_version = session.scalar(
            select(func.max(LearnedModelProfile.version)).where(
                LearnedModelProfile.participant_id == row.id
            )
        )
        last_message = session.scalar(
            select(func.max(BotEvent.received_at)).where(BotEvent.participant_id == row.id)
        )
        last_forecast = session.execute(
            select(ForecastSnapshot)
            .where(ForecastSnapshot.participant_id == row.id, ForecastSnapshot.valid.is_(True))
            .order_by(desc(ForecastSnapshot.local_date), desc(ForecastSnapshot.generated_at))
            .limit(1)
        ).scalar_one_or_none()
        latest_warning = session.execute(
            select(WarningSchedule.status)
            .where(WarningSchedule.participant_id == row.id)
            .order_by(desc(WarningSchedule.updated_at))
            .limit(1)
        ).scalar_one_or_none()
        return {
            "participant_code": row.participant_code,
            "status": row.status,
            "feishu_bound": bool(binding),
            "calendar_usable": bool(
                token and token.refresh_token_expires_at
                and token.refresh_token_expires_at > datetime.now(timezone.utc)
            ),
            "profile_version": profile_version,
            "learned_profile_version": learned_version,
            "last_message_at": _iso(last_message),
            "last_forecast_date": _iso(last_forecast.local_date) if last_forecast else None,
            "latest_warning_state": latest_warning,
            "created_at": _iso(row.created_at),
        }

    def _participant_detail(self, session, row: Participant) -> dict[str, Any]:
        result = self._participant_row(session, row)
        profile = session.execute(
            select(ParticipantProfile)
            .where(ParticipantProfile.participant_id == row.id)
            .order_by(desc(ParticipantProfile.version))
            .limit(1)
        ).scalar_one_or_none()
        learned = session.execute(
            select(LearnedModelProfile)
            .where(LearnedModelProfile.participant_id == row.id)
            .order_by(desc(LearnedModelProfile.version))
            .limit(1)
        ).scalar_one_or_none()
        observation = session.execute(
            select(StateObservation)
            .where(StateObservation.participant_id == row.id)
            .order_by(desc(StateObservation.observed_at))
            .limit(1)
        ).scalar_one_or_none()
        result.update(
            {
                "external_llm_consent": bool(row.external_llm_consent_at),
                "profile": _redact(dict(profile.profile_json)) if profile else None,
                "learned_profile": (
                    {
                        "version": learned.version,
                        "parameters": _redact(dict(learned.parameters_json)),
                        "confidence": learned.confidence,
                        "sample_count": learned.sample_count,
                        "day_count": learned.day_count,
                    }
                    if learned else None
                ),
                "latest_observation": self._observation_view(observation) if observation else None,
            }
        )
        return result

    def messages(
        self,
        participant_id: uuid.UUID,
        *,
        status: str = "",
        error_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions = [BotEvent.participant_id == participant_id]
        if status:
            conditions.append(BotEvent.status == status[:32])
        if error_only:
            conditions.append(BotEvent.error_code.is_not(None))
        with self.database.session() as session:
            rows = session.execute(
                select(BotEvent)
                .where(*conditions)
                .order_by(desc(BotEvent.received_at))
                .limit(max(1, min(limit, 200)))
            ).scalars().all()
            return [self._message_view(row) for row in rows]

    def message(self, event_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id)
            if row is None:
                return None
            runs = session.execute(
                select(AgentRun).where(
                    AgentRun.participant_id == row.participant_id,
                    or_(
                        AgentRun.message_id == row.message_id,
                        AgentRun.message_id == row.event_id,
                    ),
                ).order_by(AgentRun.started_at)
            ).scalars().all()
            run_views = []
            for run in runs:
                tools = session.execute(
                    select(AgentToolCall)
                    .where(AgentToolCall.agent_run_id == run.id)
                    .order_by(AgentToolCall.created_at)
                ).scalars().all()
                run_views.append(
                    {
                        "id": str(run.id),
                        "model": run.model,
                        "skill_version": run.skill_version,
                        "status": run.status,
                        "started_at": _iso(run.started_at),
                        "finished_at": _iso(run.finished_at),
                        "tool_calls": [
                            {
                                "tool_name": tool.tool_name,
                                "status": tool.status,
                                "arguments": _redact(dict(tool.arguments_summary_json or {})),
                                "result": _redact(dict(tool.result_summary_json or {})),
                                "created_at": _iso(tool.created_at),
                            }
                            for tool in tools
                        ],
                    }
                )
            return {**self._message_view(row), "agent_runs": run_views}

    @staticmethod
    def _message_view(row: BotEvent) -> dict[str, Any]:
        return {
            "event_id": row.event_id,
            "message_id": row.message_id,
            "received_at": _iso(row.received_at),
            "processed_at": _iso(row.processed_at),
            "text": row.text,
            "status": row.status,
            "attempts": row.attempts,
            "error_code": row.error_code,
            "reply_text": row.reply_text,
            "segment_count": len(row.reply_segments_json or []),
            "reply_message_ids": list(row.reply_message_ids_json or []),
            "telemetry": _redact(dict(row.telemetry_json or {})),
        }

    def observations(self, participant_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(StateObservation)
                .where(StateObservation.participant_id == participant_id)
                .order_by(desc(StateObservation.observed_at))
                .limit(max(1, min(limit, 500)))
            ).scalars().all()
            return [self._observation_view(row) for row in rows]

    @staticmethod
    def _observation_view(row: StateObservation) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "type": row.observation_type,
            "payload": _redact(dict(row.payload_json)),
            "observed_at": _iso(row.observed_at),
        }

    def calendars(self, participant_id: uuid.UUID, limit: int = 31) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(CalendarSnapshot)
                .where(CalendarSnapshot.participant_id == participant_id)
                .order_by(desc(CalendarSnapshot.local_date))
                .limit(max(1, min(limit, 366)))
            ).scalars().all()
            return [
                {
                    "local_date": row.local_date.isoformat(),
                    "calendar_revision": row.calendar_revision,
                    "snapshot_state": row.snapshot_state,
                    "degraded": row.degraded,
                    "last_refresh_success_at": _iso(row.last_refresh_success_at),
                    "last_refresh_error_class": row.last_refresh_error_class,
                    "events": _redact(list(row.events_json)),
                }
                for row in rows
            ]

    def forecasts(self, participant_id: uuid.UUID, limit: int = 31) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(ForecastSnapshot)
                .where(ForecastSnapshot.participant_id == participant_id)
                .order_by(desc(ForecastSnapshot.local_date), desc(ForecastSnapshot.generated_at))
                .limit(max(1, min(limit, 366)))
            ).scalars().all()
            return [self._forecast_view(row, include_curve=False) for row in rows]

    def forecast(self, participant_id: uuid.UUID, local_date: date) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.execute(
                select(ForecastSnapshot)
                .where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date == local_date,
                    ForecastSnapshot.valid.is_(True),
                )
                .order_by(desc(ForecastSnapshot.generated_at))
                .limit(1)
            ).scalar_one_or_none()
            return self._forecast_view(row, include_curve=True) if row else None

    @staticmethod
    def _forecast_view(row: ForecastSnapshot, *, include_curve: bool) -> dict[str, Any]:
        output = _redact(dict(row.output_json))
        result = {
            "id": str(row.id),
            "local_date": row.local_date.isoformat(),
            "forecast_version": row.forecast_version,
            "algorithm_version": row.algorithm_version,
            "semantic_status": row.semantic_status,
            "calendar_revision": row.calendar_revision,
            "observation_revision": row.observation_revision,
            "warning_windows": list(row.warning_windows_json),
            "peaks": list(row.peaks_json),
            "output": output,
            "initial_state": dict(output.get("initial_state") or {}),
            "initial_state_revision": output.get("initial_state_revision"),
            "valid": row.valid,
            "generated_at": _iso(row.generated_at),
        }
        if include_curve:
            result["curve"] = _redact(list(row.curve_json))
        return result

    def warnings(self, participant_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(WarningSchedule)
                .where(WarningSchedule.participant_id == participant_id)
                .order_by(desc(WarningSchedule.target_time))
                .limit(max(1, min(limit, 500)))
            ).scalars().all()
            return [
                {
                    "id": str(row.id),
                    "local_date": row.local_date.isoformat(),
                    "forecast_version": row.forecast_version,
                    "target_time": _iso(row.target_time),
                    "risk_time": _iso(row.risk_time),
                    "valid_until": _iso(row.valid_until),
                    "warning_level": row.warning_level,
                    "status": row.status,
                    "attempt_count": row.attempt_count,
                    "sent_at": _iso(row.sent_at),
                    "last_error_code": row.last_error_code,
                    "last_error_class": row.last_error_class,
                    "payload": _redact(dict(row.payload_json)),
                }
                for row in rows
            ]

    def care_timeline(
        self, participant_id: uuid.UUID, limit: int = 100
    ) -> dict[str, Any]:
        """Return provenance-complete interventions and append-only feedback."""

        with self.database.session() as session:
            preference = session.get(ParticipantCarePreference, participant_id)
            rows = session.execute(
                select(CareInterventionEvent)
                .where(CareInterventionEvent.participant_id == participant_id)
                .order_by(
                    desc(CareInterventionEvent.scheduled_at),
                    desc(CareInterventionEvent.created_at),
                )
                .limit(max(1, min(limit, 500)))
            ).scalars().all()
            event_ids = [row.id for row in rows]
            feedback_rows = (
                session.execute(
                    select(CareInterventionFeedback)
                    .where(
                        CareInterventionFeedback.intervention_id.in_(event_ids)
                    )
                    .order_by(CareInterventionFeedback.submitted_at)
                ).scalars().all()
                if event_ids
                else []
            )
            feedback_by_event: dict[uuid.UUID, list[dict[str, Any]]] = {}
            for feedback in feedback_rows:
                feedback_by_event.setdefault(feedback.intervention_id, []).append(
                    {
                        "id": str(feedback.id),
                        "helpfulness": feedback.helpfulness,
                        "relevance": feedback.relevance,
                        "timing_feedback": feedback.timing_feedback,
                        "action_selected": feedback.action_selected,
                        "optional_comment": feedback.optional_comment,
                        "submitted_at": _iso(feedback.submitted_at),
                        "callback_event_id": feedback.callback_event_id,
                    }
                )
            return {
                "preferences": self._care_preference_view(preference),
                "items": [
                    {
                        "id": str(row.id),
                        "source_warning_id": str(row.source_warning_id),
                        "source_forecast_id": str(row.source_forecast_id),
                        "forecast_version": row.forecast_version,
                        "intervention_type": row.intervention_type,
                        "template_id": row.template_id,
                        "template_version": row.template_version,
                        "reason_code": row.reason_code,
                        "scheduled_at": _iso(row.scheduled_at),
                        "sent_at": _iso(row.sent_at),
                        "status": row.status,
                        "delivery_status": row.delivery_status,
                        "user_action": row.user_action,
                        "action_at": _iso(row.action_at),
                        "snoozed_until": _iso(row.snoozed_until),
                        "message": row.message_text,
                        "context": _redact(dict(row.context_json)),
                        "actions": list(row.actions_json),
                        "feedback": feedback_by_event.get(row.id, []),
                        "created_at": _iso(row.created_at),
                        "updated_at": _iso(row.updated_at),
                    }
                    for row in rows
                ],
            }

    @staticmethod
    def _care_preference_view(
        row: ParticipantCarePreference | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "care_enabled": bool(row.care_enabled),
            "warning_enabled": bool(row.warning_enabled),
            "daily_review_enabled": bool(row.daily_review_enabled),
            "morning_brief_enabled": bool(row.morning_brief_enabled),
            "weekly_summary_enabled": bool(row.weekly_summary_enabled),
            "quiet_hours_start": (
                row.quiet_hours_start.strftime("%H:%M")
                if row.quiet_hours_start else None
            ),
            "quiet_hours_end": (
                row.quiet_hours_end.strftime("%H:%M")
                if row.quiet_hours_end else None
            ),
            "max_proactive_care_per_day": row.max_proactive_care_per_day,
            "allow_schedule_suggestions": bool(row.allow_schedule_suggestions),
            "allow_follow_up": bool(row.allow_follow_up),
            "preferred_support_types": list(row.preferred_support_types or []),
            "muted_until": _iso(row.muted_until),
            "version": row.version,
            "updated_at": _iso(row.updated_at),
        }

    def incidents(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(RuntimeIncident)
                .order_by(desc(RuntimeIncident.created_at))
                .limit(max(1, min(limit, 500)))
            ).scalars().all()
            return [
                {
                    "id": str(row.id),
                    "severity": row.severity,
                    "subsystem": row.subsystem,
                    "event_name": row.event_name,
                    "participant_id": str(row.participant_id) if row.participant_id else None,
                    "bot_event_id": row.bot_event_id,
                    "error_code": row.error_code,
                    "error_class": row.error_class,
                    "summary": row.summary,
                    "details": _redact(dict(row.details_json)),
                    "created_at": _iso(row.created_at),
                }
                for row in rows
            ]
