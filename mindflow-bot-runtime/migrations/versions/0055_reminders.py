"""Add participant-owned reminders.

Revision ID: 0055_reminders
Revises: 0054_morning_brief
"""

from alembic import op
import sqlalchemy as sa

revision = "0055_reminders"
down_revision = "0054_morning_brief"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("remind_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recurrence_type", sa.String(length=16), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_count", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("recurrence_type IN ('none', 'daily', 'weekly')", name="ck_reminder_recurrence"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reminder_due", "reminders", ["status", "next_fire_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_reminder_due", table_name="reminders")
    op.drop_table("reminders")
