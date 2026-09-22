"""Add short-lived controlled web-search evidence cache.

Revision ID: 0057_controlled_web_search
Revises: 0056_supportive_followups
"""

from alembic import op
import sqlalchemy as sa

revision = "0057_controlled_web_search"
down_revision = "0056_supportive_followups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_search_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_query", sa.String(length=500), nullable=False),
        sa.Column("freshness", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "web_search_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["web_search_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_web_search_result_run", "web_search_results", ["run_id", "rank"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_web_search_result_run", table_name="web_search_results")
    op.drop_table("web_search_results")
    op.drop_table("web_search_runs")
