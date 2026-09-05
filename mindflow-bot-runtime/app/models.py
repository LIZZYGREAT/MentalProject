"""Durable, participant-scoped production data model."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db import Base


JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _warning_authorization_deadline(context: object) -> datetime:
    """Keep legacy constructors compatible while defaulting deadline to risk."""

    return context.get_current_parameters()["risk_time"]  # type: ignore[attr-defined]


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
    refresh_lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refresh_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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


class PsychometricAssessment(Base):
    """Append-only administration history for validated psychometric instruments."""

    __tablename__ = "psychometric_assessments"
    __table_args__ = (
        Index(
            "ix_psychometric_participant_time",
            "participant_id",
            "administered_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    instrument_name: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_version: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_items_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    scores_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    administered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_period: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ParticipantSlowState(Base):
    """Append-only, derived state whose expected cadence is daily or weekly."""

    __tablename__ = "participant_slow_states"
    __table_args__ = (
        Index("ix_slow_state_participant_time", "participant_id", "effective_at"),
        CheckConstraint("cadence IN ('daily', 'weekly')", name="ck_slow_state_cadence"),
        CheckConstraint(
            "rolling_7d_stress IS NULL OR "
            "(rolling_7d_stress >= 0 AND rolling_7d_stress <= 10)",
            name="ck_slow_state_stress",
        ),
        CheckConstraint(
            "rolling_7d_workload IS NULL OR "
            "(rolling_7d_workload >= 0 AND rolling_7d_workload <= 10)",
            name="ck_slow_state_workload",
        ),
        CheckConstraint(
            "rolling_7d_energy IS NULL OR "
            "(rolling_7d_energy >= 0 AND rolling_7d_energy <= 10)",
            name="ck_slow_state_energy",
        ),
        CheckConstraint(
            "recent_recovery_quality IS NULL OR "
            "(recent_recovery_quality >= 0 AND recent_recovery_quality <= 10)",
            name="ck_slow_state_recovery",
        ),
        CheckConstraint(
            "recent_sleep_debt IS NULL OR "
            "(recent_sleep_debt >= 0 AND recent_sleep_debt <= 24)",
            name="ck_slow_state_sleep_debt",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cadence: Mapped[str] = mapped_column(String(16), nullable=False)
    rolling_7d_stress: Mapped[float | None] = mapped_column(Float, nullable=True)
    rolling_7d_workload: Mapped[float | None] = mapped_column(Float, nullable=True)
    rolling_7d_energy: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_recovery_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_sleep_debt: Mapped[float | None] = mapped_column(Float, nullable=True)
    exam_period_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class EventAppraisalFeedback(Base):
    """Participant feedback used to calibrate event semantics and workload."""

    __tablename__ = "event_appraisal_feedback"
    __table_args__ = (
        Index("ix_event_appraisal_participant_time", "participant_id", "submitted_at"),
        Index("ix_event_appraisal_event_type", "event_type", "submitted_at"),
        Index("ix_event_appraisal_course", "course_name", "submitted_at"),
        Index(
            "ix_event_appraisal_participant_event_date",
            "participant_id",
            "event_local_date",
        ),
        Index("ix_event_appraisal_source_forecast", "source_forecast_id"),
        CheckConstraint("mental_demand >= 0 AND mental_demand <= 10", name="ck_event_appraisal_mental"),
        CheckConstraint("physical_demand >= 0 AND physical_demand <= 10", name="ck_event_appraisal_physical"),
        CheckConstraint("temporal_demand >= 0 AND temporal_demand <= 10", name="ck_event_appraisal_temporal"),
        CheckConstraint("effort >= 0 AND effort <= 10", name="ck_event_appraisal_effort"),
        CheckConstraint("frustration >= 0 AND frustration <= 10", name="ck_event_appraisal_frustration"),
        CheckConstraint("perceived_control >= 0 AND perceived_control <= 10", name="ck_event_appraisal_control"),
        CheckConstraint("actual_stress >= 0 AND actual_stress <= 10", name="ck_event_appraisal_stress"),
        CheckConstraint("perceived_performance >= 0 AND perceived_performance <= 10", name="ck_event_appraisal_performance"),
        CheckConstraint(
            "workload_prior IS NULL OR (workload_prior >= 0 AND workload_prior <= 1)",
            name="ck_event_appraisal_workload_prior",
        ),
        CheckConstraint(
            "observed_workload IS NULL OR (observed_workload >= 0 AND observed_workload <= 1)",
            name="ck_event_appraisal_observed_workload",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    mental_demand: Mapped[float] = mapped_column(Float, nullable=False)
    physical_demand: Mapped[float] = mapped_column(Float, nullable=False)
    temporal_demand: Mapped[float] = mapped_column(Float, nullable=False)
    effort: Mapped[float] = mapped_column(Float, nullable=False)
    frustration: Mapped[float] = mapped_column(Float, nullable=False)
    perceived_control: Mapped[float] = mapped_column(Float, nullable=False)
    actual_stress: Mapped[float] = mapped_column(Float, nullable=False)
    perceived_performance: Mapped[float] = mapped_column(Float, nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    course_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workload_feature_vector: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    workload_prior: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_workload: Mapped[float | None] = mapped_column(Float, nullable=True)
    workload_residual: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    event_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_forecast_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("forecast_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_forecast_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_semantic_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workload_schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workload_model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
        CheckConstraint(
            "validation_status IN ('candidate', 'validated', 'rejected')",
            name="ck_learned_profile_validation_status",
        ),
        CheckConstraint("sample_count >= 0", name="ck_learned_profile_sample_count"),
        CheckConstraint("day_count >= 0", name="ck_learned_profile_day_count"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_learned_profile_confidence",
        ),
        CheckConstraint(
            "window_start <= window_end",
            name="ck_learned_profile_window",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    uncertainty_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="calibration.v1")
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="legacy")
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
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
    message_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    image_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
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


class CourseScheduleImport(Base):
    __tablename__ = "course_schedule_imports"
    __table_args__ = (
        UniqueConstraint(
            "participant_id", "source_message_id",
            name="uq_course_schedule_import_source",
        ),
        CheckConstraint(
            "status IN ('pending_context','pending_confirmation','running','succeeded',"
            "'partial_failed','cancelled','expired')",
            name="ck_course_schedule_import_status",
        ),
        Index(
            "ix_course_schedule_import_participant_status",
            "participant_id", "status", "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_image_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    semester_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    vision_model: Mapped[str] = mapped_column(String(128), nullable=False)
    structured_result: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CourseScheduleImportItem(Base):
    __tablename__ = "course_schedule_import_items"
    __table_args__ = (
        UniqueConstraint("import_id", "item_index", name="uq_course_schedule_item_index"),
        UniqueConstraint("import_id", "normalized_key", name="uq_course_schedule_item_key"),
        CheckConstraint(
            "weekday IS NULL OR weekday BETWEEN 1 AND 7",
            name="ck_course_schedule_item_weekday",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="ck_course_schedule_item_status",
        ),
        Index(
            "ix_course_schedule_item_import_status",
            "import_id", "status", "item_index",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    import_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("course_schedule_imports.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    course_name: Mapped[str] = mapped_column(String(200), nullable=False)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    week_rule_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


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
    snapshot_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="current"
    )
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_refresh_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refresh_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refresh_error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class CalendarMutationReconciliation(Base):
    """Durable local work required after a remote Calendar mutation commits."""

    __tablename__ = "calendar_mutation_reconciliations"
    __table_args__ = (
        Index(
            "ix_calendar_mutation_reconciliation_due",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_calendar_mutation_reconciliation_participant",
            "participant_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    mutation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    work_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prepared"
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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


class ForecastCurrentnessEvent(Base):
    """Append-only history of which forecast was current at a point in time."""

    __tablename__ = "forecast_currentness_events"
    __table_args__ = (
        Index(
            "ix_forecast_currentness_at",
            "participant_id",
            "local_date",
            "occurred_at",
            "id",
        ),
        Index("ix_forecast_currentness_forecast", "forecast_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("forecast_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    forecast_version: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ForecastObservationMatch(Base):
    """Causal alignment between an EMA and a five-minute forecast point."""

    __tablename__ = "forecast_observation_matches"
    __table_args__ = (
        UniqueConstraint(
            "observation_id",
            "match_schema_version",
            name="uq_forecast_observation_match_schema",
        ),
        Index(
            "ix_forecast_observation_match_participant_day",
            "participant_id",
            "local_date",
            "observed_at",
        ),
        CheckConstraint(
            "actual_stress >= 0 AND actual_stress <= 10",
            name="ck_forecast_match_actual_stress",
        ),
        CheckConstraint(
            "predicted_stress >= 0 AND predicted_stress <= 10",
            name="ck_forecast_match_predicted_stress",
        ),
        CheckConstraint(
            "prediction_lower IS NULL OR "
            "(prediction_lower >= 0 AND prediction_lower <= 10)",
            name="ck_forecast_match_prediction_lower",
        ),
        CheckConstraint(
            "prediction_upper IS NULL OR "
            "(prediction_upper >= 0 AND prediction_upper <= 10)",
            name="ck_forecast_match_prediction_upper",
        ),
        CheckConstraint(
            "prediction_lower IS NULL OR prediction_upper IS NULL OR "
            "prediction_lower <= prediction_upper",
            name="ck_forecast_match_interval_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("forecast_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    forecast_version: Mapped[str] = mapped_column(String(64), nullable=False)
    match_schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    forecast_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("state_observations.id", ondelete="CASCADE"),
        nullable=False,
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    predicted_stress: Mapped[float] = mapped_column(Float, nullable=False)
    actual_stress: Mapped[float] = mapped_column(Float, nullable=False)
    residual: Mapped[float] = mapped_column(Float, nullable=False)
    prediction_lower: Mapped[float | None] = mapped_column(Float, nullable=True)
    prediction_upper: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DatasetSnapshot(Base):
    """Immutable manifest binding future training and evaluation to data cutoffs."""

    __tablename__ = "dataset_snapshots"
    __table_args__ = (
        Index("ix_dataset_snapshot_created", "created_at"),
        Index(
            "uq_dataset_snapshot_weekly_batch",
            "purpose",
            "schedule_key",
            unique=True,
            postgresql_where=text(
                "purpose = 'stage5_weekly_calibration'"
            ),
            sqlite_where=text(
                "purpose = 'stage5_weekly_calibration'"
            ),
        ),
        CheckConstraint("date_start <= date_end", name="ck_dataset_snapshot_dates"),
        CheckConstraint(
            "(purpose = 'stage5_weekly_calibration' AND schedule_key IS NOT NULL) "
            "OR (purpose <> 'stage5_weekly_calibration' AND schedule_key IS NULL)",
            name="ck_dataset_snapshot_batch_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    date_start: Mapped[date] = mapped_column(Date, nullable=False)
    date_end: Mapped[date] = mapped_column(Date, nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(64), nullable=False, default="manual_research"
    )
    schedule_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    participant_filter: Mapped[dict] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    observation_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    calendar_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)


class DatasetSnapshotItem(Base):
    """Immutable source identity and representation frozen into a dataset."""

    __tablename__ = "dataset_snapshot_items"
    __table_args__ = (
        UniqueConstraint(
            "dataset_snapshot_id",
            "item_type",
            "source_id",
            "source_version",
            name="uq_dataset_snapshot_item_source",
        ),
        Index(
            "ix_dataset_snapshot_item_snapshot_type",
            "dataset_snapshot_id",
            "item_type",
        ),
        Index(
            "ix_dataset_snapshot_item_participant_day",
            "participant_id",
            "local_date",
        ),
        CheckConstraint(
            "item_type IN ('participant', 'observation', 'forecast', "
            "'forecast_currentness', 'calendar', 'match_source', "
            "'psychometric', 'daily_review', 'slow_state', "
            "'care_intervention_exposure', 'warning_delivery', "
            "'participant_profile', 'learned_model_profile')",
            name="ck_dataset_snapshot_item_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("dataset_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ModelEvaluationRun(Base):
    """Versioned evaluation result tied to one immutable dataset snapshot."""

    __tablename__ = "model_evaluation_runs"
    __table_args__ = (
        Index(
            "ix_model_evaluation_snapshot_created",
            "dataset_snapshot_id",
            "created_at",
        ),
        Index(
            "ix_model_evaluation_model_created",
            "model_version",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', "
            "'not_implemented')",
            name="ck_model_evaluation_status",
        ),
        CheckConstraint(
            "evaluation_mode IN ('historical_online', 'offline_replay')",
            name="ck_model_evaluation_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("dataset_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_code_version: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    participant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="SET NULL"),
        nullable=True,
    )
    metrics_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )


class ModelPromotionDecision(Base):
    """Durable proof that one candidate passed the production gate."""

    __tablename__ = "model_promotion_decisions"
    __table_args__ = (
        UniqueConstraint(
            "model_evaluation_run_id",
            "participant_id",
            "model_family",
            name="uq_model_promotion_run_participant_family",
        ),
        Index(
            "ix_model_promotion_participant_promoted",
            "participant_id",
            "promoted_at",
        ),
        Index(
            "uq_model_promotion_cohort_run_family",
            "model_evaluation_run_id",
            "model_family",
            unique=True,
            postgresql_where=text("participant_id IS NULL"),
            sqlite_where=text("participant_id IS NULL"),
        ),
        CheckConstraint(
            "status = 'retained_from_empirical_evidence'",
            name="ck_model_promotion_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("model_evaluation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dataset_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("dataset_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    participant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="RESTRICT"),
        nullable=True,
    )
    model_family: Mapped[str] = mapped_column(String(32), nullable=False)
    promotion_gate_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluation_code_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    passed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ParameterLearningRun(Base):
    """Auditable Stage-5 candidate training and promotion workflow."""

    __tablename__ = "parameter_learning_runs"
    __table_args__ = (
        Index(
            "ix_parameter_learning_participant_created",
            "participant_id",
            "created_at",
        ),
        Index(
            "ix_parameter_learning_snapshot_created",
            "dataset_snapshot_id",
            "created_at",
        ),
        Index(
            "uq_parameter_learning_scheduled_week",
            "participant_id",
            "model_family",
            "schedule_key",
            unique=True,
            postgresql_where=text("run_kind = 'scheduled'"),
            sqlite_where=text("run_kind = 'scheduled'"),
        ),
        CheckConstraint(
            "status IN ('candidate', 'rejected', 'promoted')",
            name="ck_parameter_learning_status",
        ),
        CheckConstraint(
            "run_kind IN ('manual', 'scheduled')",
            name="ck_parameter_learning_run_kind",
        ),
        CheckConstraint(
            "(run_kind = 'manual' AND schedule_key IS NULL) OR "
            "(run_kind = 'scheduled' AND schedule_key IS NOT NULL)",
            name="ck_parameter_learning_schedule_key",
        ),
        CheckConstraint(
            "sample_count >= 0",
            name="ck_parameter_learning_sample_count",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dataset_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("dataset_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    run_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual"
    )
    schedule_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parameters_before: Mapped[dict] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    parameters_candidate: Mapped[dict] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    training_metrics: Mapped[dict] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    validation_metrics: Mapped[dict] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="candidate"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


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
    snoozed_from_intervention_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("care_intervention_events.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    warning_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    episode_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    target_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    risk_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorization_deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_warning_authorization_deadline,
    )
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
    authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ParticipantCarePreference(Base):
    __tablename__ = "participant_care_preferences"
    __table_args__ = (
        CheckConstraint(
            "interruption_tolerance BETWEEN 0 AND 1",
            name="ck_care_interruption_tolerance",
        ),
    )

    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    care_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    warning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    daily_review_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    morning_brief_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    weekly_summary_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quiet_hours_start: Mapped[time | None] = mapped_column(Time(), nullable=True)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time(), nullable=True)
    max_proactive_care_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_schedule_suggestions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_follow_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    preferred_support_types: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)
    inferred_support_types: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)
    disabled_intervention_types: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)
    interruption_tolerance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    preferred_reminder_windows: Mapped[list] = mapped_column(JSON_VALUE, nullable=False, default=list)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class CareInterventionEvent(Base):
    __tablename__ = "care_intervention_events"
    __table_args__ = (
        UniqueConstraint("source_warning_id", name="uq_care_intervention_warning"),
        Index("ix_care_intervention_participant_scheduled", "participant_id", "scheduled_at"),
        Index("ix_care_intervention_status_scheduled", "status", "scheduled_at"),
        CheckConstraint(
            "(vulnerability_score IS NULL OR vulnerability_score BETWEEN 0 AND 1) AND "
            "(receptivity_score IS NULL OR receptivity_score BETWEEN 0 AND 1) AND "
            "(decision_score IS NULL OR decision_score BETWEEN 0 AND 1)",
            name="ck_care_jitai_scores",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_warning_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("warning_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_forecast_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("forecast_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    forecast_version: Mapped[str] = mapped_column(String(64), nullable=False)
    intervention_type: Mapped[str] = mapped_column(String(64), nullable=False)
    template_id: Mapped[str] = mapped_column(String(128), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    vulnerability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    receptivity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    delivery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    user_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    actions_json: Mapped[list] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class CareInterventionFeedback(Base):
    __tablename__ = "care_intervention_feedback"
    __table_args__ = (
        Index("ix_care_feedback_participant_submitted", "participant_id", "submitted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    intervention_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("care_intervention_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    helpfulness: Mapped[str | None] = mapped_column(String(32), nullable=True)
    relevance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timing_feedback: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_selected: Mapped[str] = mapped_column(String(32), nullable=False)
    optional_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    callback_event_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)


class CareInterventionOutcome(Base):
    """Observed proximal outcomes; these rows never imply causal effect."""

    __tablename__ = "care_intervention_outcomes"
    __table_args__ = (
        Index("ix_care_outcome_participant_created", "participant_id", "created_at"),
        CheckConstraint(
            "helpful_rating IS NULL OR helpful_rating BETWEEN 0 AND 1",
            name="ck_care_outcome_helpful",
        ),
    )

    intervention_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("care_intervention_events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    baseline_state: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    followup_30m: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    followup_60m: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    helpful_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    user_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    context_json: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class InterventionRandomizationEvent(Base):
    """MRT-ready audit shape; runtime randomization remains disabled in Stage 6."""

    __tablename__ = "intervention_randomization_events"
    __table_args__ = (
        Index("ix_mrt_participant_decision", "participant_id", "decision_time"),
        CheckConstraint(
            "randomization_probability BETWEEN 0 AND 1",
            name="ck_mrt_probability",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    eligibility: Mapped[bool] = mapped_column(Boolean, nullable=False)
    context: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)
    candidate_actions: Mapped[list] = mapped_column(JSON_VALUE, nullable=False)
    assigned_action: Mapped[str] = mapped_column(String(64), nullable=False)
    randomization_probability: Mapped[float] = mapped_column(Float, nullable=False)
    proximal_outcome: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


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
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    causal_source_forecast_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("forecast_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    causal_source_forecast_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    callback_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_stress: Mapped[float] = mapped_column(Float, nullable=False)
    start_energy: Mapped[float] = mapped_column(Float, nullable=False)
    peak_stress: Mapped[float] = mapped_column(Float, nullable=False)
    peak_period: Mapped[str] = mapped_column(String(32), nullable=False)
    end_stress: Mapped[float] = mapped_column(Float, nullable=False)
    end_energy: Mapped[float] = mapped_column(Float, nullable=False)
    energy_consumption: Mapped[float | None] = mapped_column(Float, nullable=True)
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
