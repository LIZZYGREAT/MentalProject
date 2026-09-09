"""Persist course import completion presentation delivery state.

Revision ID: 0043_course_schedule_completion_presentation
Revises: 0042_course_schedule_provider_identity_conflict
"""

from alembic import op
import sqlalchemy as sa


revision = "0043_course_schedule_completion_presentation"
down_revision = "0042_course_schedule_provider_identity_conflict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "course_schedule_imports",
        sa.Column("completion_presented_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "course_schedule_imports",
        sa.Column("completion_presentation_error", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("course_schedule_imports", "completion_presentation_error")
    op.drop_column("course_schedule_imports", "completion_presented_at")
