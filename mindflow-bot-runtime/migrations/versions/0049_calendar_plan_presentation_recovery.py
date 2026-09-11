"""Persist Calendar mutation plan completion presentation recovery state.

Revision ID: 0049_calendar_plan_presentation_recovery
Revises: 0048_calendar_mutation_plan_items
"""

from alembic import op
import sqlalchemy as sa


revision = "0049_calendar_plan_presentation_recovery"
down_revision = "0048_calendar_mutation_plan_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_mutation_plans",
        sa.Column("completion_presented_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "calendar_mutation_plans",
        sa.Column("completion_presentation_error", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "calendar_mutation_plans",
        sa.Column(
            "completion_presentation_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("calendar_mutation_plans", "completion_presentation_attempts")
    op.drop_column("calendar_mutation_plans", "completion_presentation_error")
    op.drop_column("calendar_mutation_plans", "completion_presented_at")
