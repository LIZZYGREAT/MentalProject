"""Add isolated daily-review schedules, revisions, and retrospective curves.

Revision ID: 0013_daily_review_feedback
Revises: 0012_admin_users
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_daily_review_feedback"
down_revision = "0012_admin_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_review_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("card_version", sa.String(32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(128), nullable=True),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("last_error_class", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "local_date", "card_version", name="uq_daily_review_schedule_version"),
    )
    op.create_index("ix_daily_review_schedule_due", "daily_review_schedules", ["status", "next_attempt_at"])
    op.create_table(
        "daily_review_responses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("card_version", sa.String(32), nullable=False),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("callback_event_id", sa.String(128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_stress", sa.Float(), nullable=False),
        sa.Column("start_energy", sa.Float(), nullable=False),
        sa.Column("peak_stress", sa.Float(), nullable=False),
        sa.Column("peak_period", sa.String(32), nullable=False),
        sa.Column("end_stress", sa.Float(), nullable=False),
        sa.Column("end_energy", sa.Float(), nullable=False),
        sa.Column("energy_consumption", sa.Float(), nullable=False),
        sa.Column("main_stressor", sa.Text(), nullable=True),
        sa.Column("recovery_note", sa.Text(), nullable=True),
        sa.Column("free_text", sa.Text(), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["schedule_id"], ["daily_review_schedules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "callback_event_id", name="uq_daily_review_callback"),
        sa.UniqueConstraint("participant_id", "local_date", "revision", name="uq_daily_review_revision"),
    )
    op.create_index("ix_daily_review_response_day", "daily_review_responses", ["participant_id", "local_date", "revision"])
    op.create_table(
        "retrospective_curve_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("source_forecast_id", sa.Uuid(), nullable=False),
        sa.Column("source_forecast_version", sa.String(64), nullable=False),
        sa.Column("daily_review_response_id", sa.Uuid(), nullable=False),
        sa.Column("daily_review_revision", sa.Integer(), nullable=False),
        sa.Column("observation_revision", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(64), nullable=False),
        sa.Column("reconstruction_version", sa.String(64), nullable=False),
        sa.Column("curve_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("analysis_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("diagnostics_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_forecast_id"], ["forecast_snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["daily_review_response_id"], ["daily_review_responses.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "local_date", "reconstruction_version", name="uq_retrospective_reconstruction_version"),
    )
    op.create_index("ix_retrospective_curve_day", "retrospective_curve_snapshots", ["participant_id", "local_date", "generated_at"])


def downgrade() -> None:
    op.drop_index("ix_retrospective_curve_day", table_name="retrospective_curve_snapshots")
    op.drop_table("retrospective_curve_snapshots")
    op.drop_index("ix_daily_review_response_day", table_name="daily_review_responses")
    op.drop_table("daily_review_responses")
    op.drop_index("ix_daily_review_schedule_due", table_name="daily_review_schedules")
    op.drop_table("daily_review_schedules")
