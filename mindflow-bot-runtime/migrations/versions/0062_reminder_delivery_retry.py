"""Add durable reminder delivery retry state.

Revision ID: 0062_reminder_delivery_retry
Revises: 0061_research_access_scope
"""

from alembic import op
import sqlalchemy as sa


revision = "0062_reminder_delivery_retry"
down_revision = "0061_research_access_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reminders",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reminders",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reminders",
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
    )
    op.drop_index("ix_reminder_due", table_name="reminders")
    op.create_index(
        "ix_reminder_due", "reminders",
        ["status", "next_attempt_at", "next_fire_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reminder_due", table_name="reminders")
    op.create_index(
        "ix_reminder_due", "reminders", ["status", "next_fire_at"], unique=False
    )
    op.drop_column("reminders", "last_error_code")
    op.drop_column("reminders", "next_attempt_at")
    op.drop_column("reminders", "attempt_count")
