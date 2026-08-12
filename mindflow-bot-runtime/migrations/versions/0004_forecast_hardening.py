"""Harden calendar freshness and warning delivery state.

Revision ID: 0004_forecast_hardening
Revises: 0003_forecast_pipeline
"""

from alembic import op


revision = "0004_forecast_hardening"
down_revision = "0003_forecast_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        "ALTER TABLE calendar_snapshots ADD COLUMN last_refresh_attempt_at TIMESTAMPTZ NULL",
        "ALTER TABLE calendar_snapshots ADD COLUMN last_refresh_success_at TIMESTAMPTZ NULL",
        "ALTER TABLE calendar_snapshots ADD COLUMN last_refresh_error_class VARCHAR(128) NULL",
        "ALTER TABLE warning_schedules ADD COLUMN episode_identity VARCHAR(64) NULL",
        "ALTER TABLE warning_schedules ADD COLUMN risk_time TIMESTAMPTZ NULL",
        "ALTER TABLE warning_schedules ADD COLUMN valid_until TIMESTAMPTZ NULL",
        "ALTER TABLE warning_schedules ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE warning_schedules ADD COLUMN next_attempt_at TIMESTAMPTZ NULL",
        "ALTER TABLE warning_schedules ADD COLUMN last_attempt_at TIMESTAMPTZ NULL",
        "ALTER TABLE warning_schedules ADD COLUMN last_error_code VARCHAR(128) NULL",
        "ALTER TABLE warning_schedules ADD COLUMN last_error_class VARCHAR(128) NULL",
        "ALTER TABLE warning_schedules ADD COLUMN claimed_at TIMESTAMPTZ NULL",
        "ALTER TABLE warning_schedules ADD COLUMN lease_until TIMESTAMPTZ NULL",
        "UPDATE warning_schedules SET episode_identity = warning_identity WHERE episode_identity IS NULL",
        "UPDATE warning_schedules SET risk_time = target_time WHERE risk_time IS NULL",
        "UPDATE warning_schedules SET valid_until = target_time + INTERVAL '30 minutes' WHERE valid_until IS NULL",
        "UPDATE warning_schedules SET lease_until = NOW() WHERE status = 'claimed' AND lease_until IS NULL",
        "ALTER TABLE warning_schedules ALTER COLUMN episode_identity SET NOT NULL",
        "ALTER TABLE warning_schedules ALTER COLUMN risk_time SET NOT NULL",
        "ALTER TABLE warning_schedules ALTER COLUMN valid_until SET NOT NULL",
        "CREATE INDEX ix_warning_episode ON warning_schedules(participant_id, local_date, episode_identity)",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_warning_episode")
    for column in (
        "lease_until", "claimed_at", "last_error_class", "last_error_code",
        "last_attempt_at", "next_attempt_at", "attempt_count", "valid_until",
        "risk_time", "episode_identity",
    ):
        op.execute(f"ALTER TABLE warning_schedules DROP COLUMN IF EXISTS {column}")
    for column in (
        "last_refresh_error_class", "last_refresh_success_at", "last_refresh_attempt_at",
    ):
        op.execute(f"ALTER TABLE calendar_snapshots DROP COLUMN IF EXISTS {column}")
