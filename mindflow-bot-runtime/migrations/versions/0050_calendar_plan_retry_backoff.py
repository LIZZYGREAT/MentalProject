"""Add durable retry scheduling for uncertain Calendar plan items.

Revision ID: 0050_calendar_plan_retry_backoff
Revises: 0049_calendar_plan_presentation_recovery
"""

from alembic import op
import sqlalchemy as sa


revision = "0050_calendar_plan_retry_backoff"
down_revision = "0049_calendar_plan_presentation_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_mutation_plan_items",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_calendar_mutation_plan_item_retry",
        "calendar_mutation_plan_items",
        ["plan_id", "status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_mutation_plan_item_retry",
        table_name="calendar_mutation_plan_items",
    )
    op.drop_column("calendar_mutation_plan_items", "next_retry_at")
