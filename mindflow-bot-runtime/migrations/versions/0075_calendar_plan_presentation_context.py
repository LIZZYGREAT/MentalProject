"""Persist typed Calendar plan presentation context.

Revision ID: 0075_calendar_plan_presentation_context
Revises: 0074_calendar_plan_update
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0075_calendar_plan_presentation_context"
down_revision = "0074_calendar_plan_update"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_mutation_plans",
        sa.Column(
            "presentation_context_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.alter_column(
        "calendar_mutation_plans",
        "presentation_context_json",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("calendar_mutation_plans", "presentation_context_json")
