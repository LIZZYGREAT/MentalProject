"""Add durable opt-in morning brief schedules.

Revision ID: 0054_morning_brief
Revises: 0053_proactive_notification_policy
"""

from alembic import op
import sqlalchemy as sa

revision = "0054_morning_brief"
down_revision = "0053_proactive_notification_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "morning_brief_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=256), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "local_date", name="uq_morning_brief_day"),
    )
    op.create_index("ix_morning_brief_due", "morning_brief_schedules", ["status", "scheduled_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_morning_brief_due", table_name="morning_brief_schedules")
    op.drop_table("morning_brief_schedules")
