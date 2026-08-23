"""Durable, participant-scoped production data model."""

from __future__ import annotations

from datetime import date, datetime, timezone
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db import Base


JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    student_no_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    external_llm_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ParticipantInvite(Base):
    __tablename__ = "participant_invites"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class FeishuBinding(Base):
    __tablename__ = "feishu_bindings"
    __table_args__ = (UniqueConstraint("app_id", "open_id", name="uq_feishu_app_open"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    app_id: Mapped[str] = mapped_column(String(128), nullable=False)
    open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    participant: Mapped[Participant] = relationship()


class FeishuOAuthToken(Base):
    __tablename__ = "feishu_oauth_tokens"

    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), primary_key=True
    )
    oauth_app_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    access_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    granted_scopes: Mapped[list | None] = mapped_column(JSON_VALUE, nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ParticipantProfile(Base):
    __tablename__ = "participant_profiles"
    __table_args__ = (UniqueConstraint("participant_id", "version", name="uq_profile_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class StateObservation(Base):
    __tablename__ = "state_observations"
    __table_args__ = (
        Index("ix_observation_participant_time", "participant_id", "observed_at"),
        UniqueConstraint(
            "participant_id",
            "source_message_id",
            "observation_type",
            name="uq_observation_message_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    observation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PredictionRun(Base):
    __tablename__ = "prediction_runs"
    __table_args__ = (
        Index("ix_prediction_participant_time", "participant_id", "created_at"),
        UniqueConstraint(
            "participant_id",
            "source_message_id",
            name="uq_prediction_source_message",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    output_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_participant_time", "participant_id", "created_at"),
        UniqueConstraint(
            "participant_id",
            "feishu_message_id",
            "role",
            name="uq_conversation_source_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    feishu_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class LearnedModelProfile(Base):
    """Versioned, conservative parameters learned from longitudinal evidence."""

    __tablename__ = "learned_model_profiles"
    __table_args__ = (
        UniqueConstraint("participant_id", "version", name="uq_learned_profile_version"),
        Index("ix_learned_profile_participant_version", "participant_id", "version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="calibration.v1")
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    day_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ClaudeSession(Base):
    __tablename__ = "claude_sessions"

    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    session_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class BotEvent(Base):
    __tablename__ = "bot_events"
    __table_args__ = (
        Index("ix_bot_event_participant_received", "participant_id", "received_at"),
        Index("ix_bot_event_status_received", "status", "received_at"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    app_id: Mapped[str] = mapped_column(String(128), nullable=False)
    open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_type: Mapped[str] = mapped_column(String(32), nullable=False, default="p2p")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    message_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    participant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_segments_json: Mapped[list | None] = mapped_column(JSON_VALUE, nullable=True)
    reply_next_segment: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reply_message_ids_json: Mapped[list | None] = mapped_column(JSON_VALUE, nullable=True)
    reply_plan_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reply_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    telemetry_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_run_participant_started", "participant_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_summary_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    result_summary_json: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class FeishuDeviceFlow(Base):
    __tablename__ = "feishu_device_flows"

    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), primary_key=True
    )
    oauth_app_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_code_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    user_code: Mapped[str] = mapped_column(String(128), nullable=False)
    verification_url: Mapped[str] = mapped_column(Text, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class CalendarSnapshot(Base):
    __tablename__ = "calendar_snapshots"
    __table_args__ = (
        UniqueConstraint("participant_id", "local_date", name="uq_calendar_snapshot_day"),
        Index("ix_calendar_snapshot_participant_day", "participant_id", "local_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    calendar_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    events_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_refresh_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refresh_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refresh_error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class EventSemanticCache(Base):
    __tablename__ = "event_semantic_cache"
    __table_args__ = (
        UniqueConstraint(
            "participant_id", "fingerprint", "schema_version", "prompt_version", "model",
            name="uq_semantic_participant_fingerprint",
        ),
        Index("ix_semantic_cache_participant_fingerprint", "participant_id", "fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    assessment_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ForecastSnapshot(Base):
    __tablename__ = "forecast_snapshots"
    __table_args__ = (
        UniqueConstraint("participant_id", "local_date", "forecast_version", name="uq_forecast_version"),
        Index("ix_forecast_participant_day", "participant_id", "local_date", "generated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    calendar_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    forecast_version: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_status: Mapped[str] = mapped_column(String(32), nullable=False)
    semantic_input_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False)
    curve_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False)
    peaks_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False)
    warning_windows_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False)
    output_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class WarningSchedule(Base):
    __tablename__ = "warning_schedules"
    __table_args__ = (
        UniqueConstraint("participant_id", "local_date", "warning_identity", name="uq_warning_identity"),
        Index("ix_warning_pending_target", "status", "target_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("forecast_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    forecast_version: Mapped[str] = mapped_column(String(64), nullable=False)
    warning_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    episode_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    target_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    risk_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    warning_level: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    payload_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class RuntimeIncident(Base):
    __tablename__ = "runtime_incidents"
    __table_args__ = (
        Index("ix_runtime_incident_created", "created_at"),
        Index("ix_runtime_incident_severity_created", "severity", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    subsystem: Mapped[str] = mapped_column(String(64), nullable=False)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    participant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="SET NULL"),
        nullable=True,
    )
    bot_event_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("bot_events.event_id", ondelete="SET NULL"), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AdminUser(Base):
    """Database-backed administrator; the environment account is the root of trust."""

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="viewer")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    is_environment_bootstrap: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DailyReviewSchedule(Base):
    __tablename__ = "daily_review_schedules"
    __table_args__ = (
        UniqueConstraint(
            "participant_id", "local_date", "card_version",
            name="uq_daily_review_schedule_version",
        ),
        Index("ix_daily_review_schedule_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    card_version: Mapped[str] = mapped_column(String(32), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class DailyReviewResponse(Base):
    __tablename__ = "daily_review_responses"
    __table_args__ = (
        UniqueConstraint("participant_id", "callback_event_id", name="uq_daily_review_callback"),
        UniqueConstraint("participant_id", "local_date", "revision", name="uq_daily_review_revision"),
        Index("ix_daily_review_response_day", "participant_id", "local_date", "revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    card_version: Mapped[str] = mapped_column(String(32), nullable=False)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("daily_review_schedules.id", ondelete="SET NULL"), nullable=True
    )
    callback_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_stress: Mapped[float] = mapped_column(Float, nullable=False)
    start_energy: Mapped[float] = mapped_column(Float, nullable=False)
    peak_stress: Mapped[float] = mapped_column(Float, nullable=False)
    peak_period: Mapped[str] = mapped_column(String(32), nullable=False)
    end_stress: Mapped[float] = mapped_column(Float, nullable=False)
    end_energy: Mapped[float] = mapped_column(Float, nullable=False)
    energy_consumption: Mapped[float] = mapped_column(Float, nullable=False)
    main_stressor: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    free_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class RetrospectiveCurveSnapshot(Base):
    __tablename__ = "retrospective_curve_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "participant_id", "local_date", "reconstruction_version",
            name="uq_retrospective_reconstruction_version",
        ),
        Index("ix_retrospective_curve_day", "participant_id", "local_date", "generated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_forecast_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("forecast_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    source_forecast_version: Mapped[str] = mapped_column(String(64), nullable=False)
    daily_review_response_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("daily_review_responses.id", ondelete="RESTRICT"), nullable=False
    )
    daily_review_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reconstruction_version: Mapped[str] = mapped_column(String(64), nullable=False)
    curve_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False)
    analysis_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    diagnostics_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
