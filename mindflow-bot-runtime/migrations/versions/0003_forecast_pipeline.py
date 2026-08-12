"""Durable dynamic calendar forecast and warning pipeline.

Revision ID: 0003_forecast_pipeline
Revises: 0002_claude_sessions
"""

from alembic import op


revision = "0003_forecast_pipeline"
down_revision = "0002_claude_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ddl = """
    CREATE TABLE calendar_snapshots (
        id UUID PRIMARY KEY, participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
        local_date DATE NOT NULL, calendar_revision VARCHAR(64) NOT NULL, events_json JSONB NOT NULL,
        degraded BOOLEAN NOT NULL DEFAULT false, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_calendar_snapshot_day UNIQUE(participant_id, local_date)
    );
    CREATE INDEX ix_calendar_snapshot_participant_day ON calendar_snapshots(participant_id, local_date);
    CREATE TABLE event_semantic_cache (
        id UUID PRIMARY KEY, participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
        fingerprint VARCHAR(64) NOT NULL, schema_version VARCHAR(64) NOT NULL,
        prompt_version VARCHAR(64) NOT NULL, model VARCHAR(128) NOT NULL,
        assessment_json JSONB NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'complete',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_semantic_participant_fingerprint UNIQUE(participant_id, fingerprint, schema_version, prompt_version, model)
    );
    CREATE INDEX ix_semantic_cache_participant_fingerprint ON event_semantic_cache(participant_id, fingerprint);
    CREATE TABLE forecast_snapshots (
        id UUID PRIMARY KEY, participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
        local_date DATE NOT NULL, calendar_revision VARCHAR(64) NOT NULL,
        semantic_revision VARCHAR(64) NOT NULL, algorithm_version VARCHAR(64) NOT NULL,
        forecast_version VARCHAR(64) NOT NULL, semantic_status VARCHAR(32) NOT NULL,
        semantic_input_json JSONB NOT NULL, curve_json JSONB NOT NULL, peaks_json JSONB NOT NULL,
        warning_windows_json JSONB NOT NULL, output_json JSONB NOT NULL,
        valid BOOLEAN NOT NULL DEFAULT true, generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_forecast_version UNIQUE(participant_id, local_date, forecast_version)
    );
    CREATE INDEX ix_forecast_participant_day ON forecast_snapshots(participant_id, local_date, generated_at);
    CREATE TABLE warning_schedules (
        id UUID PRIMARY KEY, participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
        local_date DATE NOT NULL, forecast_id UUID NOT NULL REFERENCES forecast_snapshots(id) ON DELETE CASCADE,
        forecast_version VARCHAR(64) NOT NULL, warning_identity VARCHAR(64) NOT NULL,
        target_time TIMESTAMPTZ NOT NULL, warning_level VARCHAR(32) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending', payload_json JSONB NOT NULL,
        sent_at TIMESTAMPTZ NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_warning_identity UNIQUE(participant_id, local_date, warning_identity)
    );
    CREATE INDEX ix_warning_pending_target ON warning_schedules(status, target_time);
    """
    for statement in ddl.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in ("warning_schedules", "forecast_snapshots", "event_semantic_cache", "calendar_snapshots"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
