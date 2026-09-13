"""Stop retaining normalized outbound search query plaintext.

Revision ID: 0063_web_search_query_privacy
Revises: 0062_reminder_delivery_retry
"""

from alembic import op
import sqlalchemy as sa


revision = "0063_web_search_query_privacy"
down_revision = "0062_reminder_delivery_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "web_search_runs", "normalized_query",
        existing_type=sa.String(length=500), nullable=True,
    )
    op.execute("UPDATE web_search_runs SET normalized_query = NULL")


def downgrade() -> None:
    op.execute("UPDATE web_search_runs SET normalized_query = '' WHERE normalized_query IS NULL")
    op.alter_column(
        "web_search_runs", "normalized_query",
        existing_type=sa.String(length=500), nullable=False,
    )
