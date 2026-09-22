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
    ParticipantConsent,
    CalendarMutationReconciliation,
    CalendarSnapshot,
    CareInterventionEvent,
    CareInterventionFeedback,
    CareInterventionOutcome,
    CourseScheduleImageSession,
    CourseScheduleImport,
    FeishuBinding,
    FeishuOAuthToken,
    ForecastSnapshot,
    LearnedModelProfile,
    Participant,
    ParticipantCarePreference,
    ParticipantMemoryItem,
    ParticipantProfile,
    ParticipantSlowState,
    PsychometricAssessment,
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

    def memory_audit(self, participant_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return provenance only; the admin surface cannot edit user memory."""

        with self.database.session() as session:
            rows = session.execute(
                select(ParticipantMemoryItem)
                .where(ParticipantMemoryItem.participant_id == participant_id)
                .order_by(desc(ParticipantMemoryItem.updated_at))
                .limit(100)
            ).scalars().all()
            return [
                {
                    "memory_type": row.memory_type,
                    "source": row.source,
                    "consent_basis": row.consent_basis,
                    "status": row.status,
                    "created_at": _iso(row.created_at),
                    "updated_at": _iso(row.updated_at),
                }
                for row in rows
            ]

    def research_state_audit(
        self, participant_id: uuid.UUID
    ) -> dict[str, list[dict[str, Any]]]:
        """Expose research-state provenance separately from explicit memory."""

        with self.database.session() as session:
            observations = session.execute(
                select(StateObservation)
                .where(StateObservation.participant_id == participant_id)
                .order_by(desc(StateObservation.observed_at))
                .limit(100)
            ).scalars().all()
            slow_states = session.execute(
                select(ParticipantSlowState)
                .where(ParticipantSlowState.participant_id == participant_id)
                .order_by(desc(ParticipantSlowState.effective_at))
                .limit(100)
            ).scalars().all()
            profiles = session.execute(
                select(LearnedModelProfile)
                .where(LearnedModelProfile.participant_id == participant_id)
                .order_by(desc(LearnedModelProfile.version))
                .limit(100)
            ).scalars().all()
            return {
                "observations": [
                    {
                        "type": row.observation_type,
                        "observed_at": _iso(row.observed_at),
                        "created_at": _iso(row.created_at),
                    }
                    for row in observations
                ],
                "slow_states": [
                    {
                        "effective_at": _iso(row.effective_at),
                        "cadence": row.cadence,
                        "source": row.source,
                        "created_at": _iso(row.created_at),
                    }
                    for row in slow_states
                ],
                "model_profiles": [
                    {
                        "version": row.version,
                        "model_version": row.model_version,
                        "validation_status": row.validation_status,
                        "sample_count": row.sample_count,
                        "window_start": _iso(row.window_start),
                        "window_end": _iso(row.window_end),
                        "created_at": _iso(row.created_at),
                    }
                    for row in profiles
                ],
            }

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
        psychometrics = session.execute(
            select(PsychometricAssessment)
            .where(PsychometricAssessment.participant_id == row.id)
            .order_by(desc(PsychometricAssessment.administered_at))
            .limit(100)
        ).scalars().all()
        slow_states = session.execute(
            select(ParticipantSlowState)
            .where(ParticipantSlowState.participant_id == row.id)
            .order_by(desc(ParticipantSlowState.effective_at))
            .limit(100)
        ).scalars().all()
        learned_history = session.execute(
            select(LearnedModelProfile)
            .where(LearnedModelProfile.participant_id == row.id)
            .order_by(desc(LearnedModelProfile.version))
            .limit(100)
        ).scalars().all()
        observation = session.execute(
            select(StateObservation)
            .where(StateObservation.participant_id == row.id)
            .order_by(desc(StateObservation.observed_at))
            .limit(1)
        ).scalar_one_or_none()
        profile_payload = dict(profile.profile_json) if profile else {}
        explicit_payload = profile_payload.get("explicit")
        explicit_data = (
            dict(explicit_payload)
            if isinstance(explicit_payload, dict)
            else {}
        )
        legacy_compatibility = {
            key: profile_payload[key]
            for key in ("model_params", "params")
            if key in profile_payload
        }
        if profile and profile_payload.get("schema_version") != "2.0":
            legacy_compatibility["legacy_profile"] = profile_payload
        result.update(
            {
                "external_llm_user_consent": (
                    self._consent_view(session, row.id)
                ),
                "legacy_external_llm_consent_at": _iso(
                    row.external_llm_consent_at
                ),
                "profile": _redact(dict(profile.profile_json)) if profile else None,
                "learned_profile": (
                    {
                        "version": learned.version,
                        "parameters": _redact(dict(learned.parameters_json)),
                        "uncertainty": _redact(dict(learned.uncertainty_json or {})),
                        "model_version": learned.model_version,
                        "validation_status": learned.validation_status,
                        "confidence": learned.confidence,
                        "sample_count": learned.sample_count,
                        "day_count": learned.day_count,
                    }
                    if learned else None
                ),
                "profile_layers": {
                    "explicit": (
                        {
                            "version": profile.version,
                            "data": _redact(explicit_data),
                            "created_at": _iso(profile.created_at),
                        }
                        if profile else None
                    ),
                    "schema_version": profile_payload.get("schema_version"),
                    "legacy_compatibility": _redact(legacy_compatibility),
                    "psychometrics": [
                        {
                            "id": str(item.id),
                            "instrument_name": item.instrument_name,
                            "instrument_version": item.instrument_version,
                            "language": item.language,
                            "scores": _redact(dict(item.scores_json)),
                            "administered_at": _iso(item.administered_at),
                            "reference_period": item.reference_period,
                            "created_at": _iso(item.created_at),
                        }
                        for item in psychometrics
                    ],
                    "slow_state": [
                        {
                            "id": str(item.id),
                            "effective_at": _iso(item.effective_at),
                            "cadence": item.cadence,
                            "rolling_7d_stress": item.rolling_7d_stress,
                            "rolling_7d_workload": item.rolling_7d_workload,
                            "rolling_7d_energy": item.rolling_7d_energy,
                            "recent_recovery_quality": item.recent_recovery_quality,
                            "recent_sleep_debt": item.recent_sleep_debt,
                            "exam_period_flag": item.exam_period_flag,
                            "source": item.source,
                            "created_at": _iso(item.created_at),
                        }
                        for item in slow_states
                    ],
                    "learned_parameters": [
                        {
                            "version": item.version,
                            "parameters": _redact(dict(item.parameters_json)),
                            "uncertainty": _redact(dict(item.uncertainty_json or {})),
                            "sample_count": item.sample_count,
                            "window_start": _iso(item.window_start),
                            "window_end": _iso(item.window_end),
                            "model_version": item.model_version,
                            "validation_status": item.validation_status,
                            "source": item.source,
                            "created_at": _iso(item.created_at),
                        }
                        for item in learned_history
                    ],
                },
                "latest_observation": self._observation_view(observation) if observation else None,
            }
        )
        return result

    @staticmethod
    def _consent_view(session: Any, participant_id: uuid.UUID) -> dict[str, Any]:
        """Authoritative participant-owned external LLM consent state.

        Read-only: the admin surface has no grant/revoke/approve entry, and
        the legacy researcher/CLI flag is exposed separately for audit only.
        """

        from app.services.consent_service import EXTERNAL_LLM_CONSENT_TYPE, EXTERNAL_LLM_CONSENT_VERSION

        current = session.execute(
            select(ParticipantConsent)
            .where(
                ParticipantConsent.participant_id == participant_id,
                ParticipantConsent.consent_type == EXTERNAL_LLM_CONSENT_TYPE,
            )
            .order_by(
                desc(ParticipantConsent.consented_at),
                desc(ParticipantConsent.created_at),
            )
            .limit(1)
        ).scalar_one_or_none()
        active = bool(
            current
            and current.status == "active"
            and current.consent_version == EXTERNAL_LLM_CONSENT_VERSION
        )
        return {
            "active": active,
            "consent_version": (
                current.consent_version
                if current is not None
                else EXTERNAL_LLM_CONSENT_VERSION
            ),
            "consented_at": _iso(current.consented_at) if current else None,
            "revoked_at": _iso(current.revoked_at) if current else None,
        }

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
        protected = row.content_privacy_class == "protected"
        return {
            "event_id": row.event_id,
            "message_id": row.message_id,
            "received_at": _iso(row.received_at),
            "processed_at": _iso(row.processed_at),
            "content_privacy_class": row.content_privacy_class,
            "content_redacted": protected,
            "text": "[内容受隐私保护]" if protected else row.text,
            "status": row.status,
            "attempts": row.attempts,
            "error_code": row.error_code,
            "reply_text": "[内容受隐私保护]" if protected else row.reply_text,
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
                    "authorization_deadline": _iso(row.authorization_deadline),
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
            outcome_rows = (
                session.execute(
                    select(CareInterventionOutcome).where(
                        CareInterventionOutcome.intervention_id.in_(event_ids)
                    )
                ).scalars().all()
                if event_ids else []
            )
            outcomes = {
                row.intervention_id: {
                    "baseline_state": _redact(dict(row.baseline_state or {})),
                    "followup_30m": _redact(dict(row.followup_30m or {})) if row.followup_30m else None,
                    "followup_60m": _redact(dict(row.followup_60m or {})) if row.followup_60m else None,
                    "helpful_rating": row.helpful_rating,
                    "user_action": row.user_action,
                    "context": _redact(dict(row.context_json or {})),
                    "created_at": _iso(row.created_at),
                    "updated_at": _iso(row.updated_at),
                }
                for row in outcome_rows
            }
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
                        "vulnerability_score": row.vulnerability_score,
                        "receptivity_score": row.receptivity_score,
                        "decision_score": row.decision_score,
                        "decision": _redact(dict(row.decision_json or {})),
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
                        "outcome": outcomes.get(row.id),
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
            "inferred_support_types": list(row.inferred_support_types or []),
            "disabled_intervention_types": list(row.disabled_intervention_types or []),
            "interruption_tolerance": row.interruption_tolerance,
            "preferred_reminder_windows": list(row.preferred_reminder_windows or []),
            "muted_until": _iso(row.muted_until),
            "version": row.version,
            "updated_at": _iso(row.updated_at),
        }

    def incidents(self, limit: int = 100) -> list[dict[str, Any]]:
        """Aggregate user-visible and operational failures across durable stores."""

        limit = max(1, min(limit, 500))
        with self.database.session() as session:
            runtime_rows = session.execute(
                select(RuntimeIncident)
                .order_by(desc(RuntimeIncident.created_at))
                .limit(limit)
            ).scalars().all()
            bot_rows = session.execute(
                select(BotEvent)
                .where(
                    or_(
                        BotEvent.error_code.is_not(None),
                        BotEvent.status.in_(
                            ("failed", "failed_replied", "reply_pending")
                        ),
                    )
                )
                .order_by(desc(BotEvent.received_at))
                .limit(limit)
            ).scalars().all()
            run_rows = session.execute(
                select(AgentRun)
                .where(AgentRun.status == "failed")
                .order_by(desc(AgentRun.started_at))
                .limit(limit)
            ).scalars().all()
            tool_rows = session.execute(
                select(AgentToolCall, AgentRun)
                .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
                .where(AgentToolCall.status != "succeeded")
                .order_by(desc(AgentToolCall.created_at))
                .limit(limit)
            ).all()
            import_rows = session.execute(
                select(CourseScheduleImport)
                .where(
                    CourseScheduleImport.status.in_(
                        ("partial_failed", "cleanup_failed")
                    )
                )
                .order_by(desc(CourseScheduleImport.created_at))
                .limit(limit)
            ).scalars().all()
            reconciliation_rows = session.execute(
                select(CalendarMutationReconciliation)
                .where(
                    or_(
                        CalendarMutationReconciliation.status.in_(
                            (
                                "remote_failed",
                                "remote_outcome_unknown",
                                "fencing_failed",
                            )
                        ),
                        (
                            CalendarMutationReconciliation.status == "resolved"
                        )
                        & (CalendarMutationReconciliation.attempt_count > 0),
                    )
                )
                .order_by(desc(CalendarMutationReconciliation.updated_at))
                .limit(limit)
            ).scalars().all()
            warning_rows = session.execute(
                select(WarningSchedule)
                .where(
                    or_(
                        WarningSchedule.last_error_code.is_not(None),
                        WarningSchedule.last_error_class.is_not(None),
                    )
                )
                .order_by(desc(WarningSchedule.updated_at))
                .limit(limit)
            ).scalars().all()
            image_rows = session.execute(
                select(CourseScheduleImageSession)
                .where(CourseScheduleImageSession.last_error_code.is_not(None))
                .order_by(desc(CourseScheduleImageSession.updated_at))
                .limit(limit)
            ).scalars().all()
            participant_codes = dict(
                session.execute(
                    select(Participant.id, Participant.participant_code)
                ).all()
            )

            items: list[dict[str, Any]] = []

            def append(
                *,
                source: str,
                source_id: str,
                created_at: datetime,
                severity: str,
                subsystem: str,
                event_name: str,
                participant_id: uuid.UUID | None,
                status: str,
                summary: str,
                error_code: str | None = None,
                error_class: str | None = None,
                recovered: bool = False,
                details: dict[str, Any] | None = None,
                **references: Any,
            ) -> None:
                items.append({
                    "id": f"{source}:{source_id}",
                    "source": source,
                    "source_id": source_id,
                    "severity": severity,
                    "subsystem": subsystem,
                    "event_name": event_name,
                    "participant_id": (
                        str(participant_id) if participant_id else None
                    ),
                    "participant_code": (
                        participant_codes.get(participant_id)
                        if participant_id else None
                    ),
                    "status": status,
                    "recovered": recovered,
                    "error_code": error_code,
                    "error_class": error_class,
                    "summary": str(summary)[:500],
                    "details": _redact(dict(details or {})),
                    "created_at": _iso(created_at),
                    "_sort_at": created_at,
                    **references,
                })

            for row in runtime_rows:
                append(
                    source="runtime_incident",
                    source_id=str(row.id),
                    created_at=row.created_at,
                    severity=row.severity,
                    subsystem=row.subsystem,
                    event_name=row.event_name,
                    participant_id=row.participant_id,
                    status="open",
                    summary=row.summary,
                    error_code=row.error_code,
                    error_class=row.error_class,
                    details=dict(row.details_json or {}),
                    bot_event_id=row.bot_event_id,
                )

            for row in bot_rows:
                append(
                    source="bot_event",
                    source_id=row.event_id,
                    created_at=row.processed_at or row.received_at,
                    severity="error",
                    subsystem="bot",
                    event_name="bot_event_failure",
                    participant_id=row.participant_id,
                    status=row.status,
                    summary=row.text or "Bot event processing failed.",
                    error_code=row.error_code or row.status,
                    details={"telemetry": dict(row.telemetry_json or {})},
                    bot_event_id=row.event_id,
                    message_id=row.message_id,
                )

            for row in run_rows:
                append(
                    source="agent_run",
                    source_id=str(row.id),
                    created_at=row.finished_at or row.started_at,
                    severity="error",
                    subsystem="agent",
                    event_name="agent_run_failed",
                    participant_id=row.participant_id,
                    status=row.status,
                    summary="Agent run did not complete successfully.",
                    error_code="agent_run_failed",
                    agent_run_id=str(row.id),
                    message_id=row.message_id,
                )

            for tool, run in tool_rows:
                result = dict(tool.result_summary_json or {})
                tool_result = dict(result.get("result") or {})
                reason_code = str(
                    result.get("reason_code")
                    or tool_result.get("reason_code")
                    or tool_result.get("error")
                    or tool.status
                )
                subsystem = (
                    "calendar"
                    if tool.tool_name.startswith("calendar_")
                    else "course_schedule"
                    if tool.tool_name.startswith("course_schedule_")
                    else "care"
                    if tool.tool_name.startswith("care_")
                    else "agent_tool"
                )
                severity = (
                    "error"
                    if tool.status in {"tool_exception", "authorization_unavailable"}
                    else "warning"
                )
                append(
                    source="agent_tool_call",
                    source_id=str(tool.id),
                    created_at=tool.created_at,
                    severity=severity,
                    subsystem=subsystem,
                    event_name=tool.tool_name,
                    participant_id=run.participant_id,
                    status=tool.status,
                    summary=f"{tool.tool_name} ended with {tool.status}.",
                    error_code=reason_code,
                    details={
                        "arguments": dict(tool.arguments_summary_json or {}),
                        "result": result,
                    },
                    agent_run_id=str(run.id),
                    message_id=run.message_id,
                )

            for row in import_rows:
                courses = list((row.structured_result or {}).get("courses") or [])
                names = [
                    str(course.get("course_name") or "未命名课程")[:80]
                    for course in courses[:3]
                ]
                append(
                    source="course_schedule_import",
                    source_id=str(row.id),
                    created_at=row.completed_at or row.created_at,
                    severity=(
                        "error" if row.status == "cleanup_failed" else "warning"
                    ),
                    subsystem="course_schedule",
                    event_name=row.status,
                    participant_id=row.participant_id,
                    status=row.status,
                    summary=(
                        "课程表导入未完全完成"
                        + (f"：{', '.join(names)}" if names else "")
                    ),
                    error_code=row.cleanup_error_code or row.status,
                    import_id=str(row.id),
                    message_id=row.source_message_id,
                )

            for row in reconciliation_rows:
                recovered = row.status == "resolved"
                append(
                    source="calendar_reconciliation",
                    source_id=str(row.id),
                    created_at=row.updated_at,
                    severity="recovered" if recovered else "error",
                    subsystem="calendar",
                    event_name="calendar_mutation_reconciliation",
                    participant_id=row.participant_id,
                    status=row.status,
                    recovered=recovered,
                    summary=f"{row.mutation_kind} reconciliation is {row.status}.",
                    error_code=row.last_error_class or row.status,
                    details={"attempt_count": row.attempt_count},
                    reconciliation_id=str(row.id),
                )

            for row in warning_rows:
                recovered = row.status == "sent"
                append(
                    source="warning_schedule",
                    source_id=str(row.id),
                    created_at=row.updated_at,
                    severity="recovered" if recovered else "warning",
                    subsystem="warning",
                    event_name="warning_delivery_failure",
                    participant_id=row.participant_id,
                    status=row.status,
                    recovered=recovered,
                    summary=f"Warning delivery for {row.local_date} is {row.status}.",
                    error_code=row.last_error_code,
                    error_class=row.last_error_class,
                    details={"attempt_count": row.attempt_count},
                    warning_id=str(row.id),
                )

            for row in image_rows:
                append(
                    source="course_schedule_image",
                    source_id=str(row.id),
                    created_at=row.updated_at,
                    severity="warning",
                    subsystem="vision",
                    event_name="course_schedule_image_failure",
                    participant_id=row.participant_id,
                    status=row.status,
                    summary=row.error_detail or "Course schedule image parsing failed.",
                    error_code=row.last_error_code,
                    details={"parse_report": dict(row.parse_report_json or {})},
                    image_session_id=str(row.id),
                    message_id=row.image_message_id,
                )

            items.sort(key=lambda item: item["_sort_at"], reverse=True)
            output = items[:limit]
            for item in output:
                item.pop("_sort_at", None)
            return output
