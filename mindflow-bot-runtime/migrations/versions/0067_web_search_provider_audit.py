"""Record the native web-search provider and synthesized evidence metadata.

Revision ID: 0067_web_search_provider_audit
Revises: 0066_admin_research_detail_scope
"""

from alembic import op
import sqlalchemy as sa


revision = "0067_web_search_provider_audit"
down_revision = "0066_admin_research_detail_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_search_runs",
        sa.Column("provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "web_search_runs",
        sa.Column("provider_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "web_search_runs",
        sa.Column("provider_request_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_search_runs", "provider_request_id")
    op.drop_column("web_search_runs", "provider_summary")
    op.drop_column("web_search_runs", "provider")
