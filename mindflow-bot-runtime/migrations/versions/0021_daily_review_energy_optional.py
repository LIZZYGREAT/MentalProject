"""Make Daily Review energy-consumption diagnostic optional.

Revision ID: 0021_daily_review_energy_optional
Revises: 0020_oauth_refresh_lease
"""

from alembic import op
import sqlalchemy as sa


revision = "0021_daily_review_energy_optional"
down_revision = "0020_oauth_refresh_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "daily_review_responses",
        "energy_consumption",
        existing_type=sa.Float(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE daily_review_responses "
        "SET energy_consumption = 0 "
        "WHERE energy_consumption IS NULL"
    )
    op.alter_column(
        "daily_review_responses",
        "energy_consumption",
        existing_type=sa.Float(),
        nullable=False,
    )
