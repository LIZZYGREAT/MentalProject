"""Add a bounded delivery window to Daily Review schedules.

Revision ID: 0014_daily_review_expiry
Revises: 0013_daily_review_feedback
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_daily_review_expiry"
down_revision = "0013_daily_review_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_review_schedules",
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE daily_review_schedules "
        "SET valid_until = scheduled_at + INTERVAL '1 day' "
        "WHERE valid_until IS NULL"
    )
    op.alter_column(
        "daily_review_schedules", "valid_until", nullable=False
    )


def downgrade() -> None:
    op.drop_column("daily_review_schedules", "valid_until")
