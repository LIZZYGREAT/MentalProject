"""Add expiring OAuth refresh leases.

Revision ID: 0020_oauth_refresh_lease
Revises: 0019_forecast_currentness_history
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_oauth_refresh_lease"
down_revision = "0019_forecast_currentness_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feishu_oauth_tokens",
        sa.Column("refresh_lease_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "feishu_oauth_tokens",
        sa.Column("refresh_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "feishu_oauth_tokens",
        sa.Column("refresh_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("feishu_oauth_tokens", "refresh_started_at")
    op.drop_column("feishu_oauth_tokens", "refresh_lease_until")
    op.drop_column("feishu_oauth_tokens", "refresh_lease_token")
