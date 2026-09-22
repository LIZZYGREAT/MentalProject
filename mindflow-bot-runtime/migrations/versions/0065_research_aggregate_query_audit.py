"""Audit fixed-bucket research aggregate queries.

Revision ID: 0065_research_aggregate_query_audit
Revises: 0064_personalization_constraints
"""

from alembic import op
import sqlalchemy as sa


revision = "0065_research_aggregate_query_audit"
down_revision = "0064_personalization_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_aggregate_query_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("researcher_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("date_start", sa.Date(), nullable=False),
        sa.Column("date_end", sa.Date(), nullable=False),
        sa.Column("cohort_size", sa.Integer(), nullable=False),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["researcher_id"], ["participants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_aggregate_audit_researcher",
        "research_aggregate_query_audit", ["researcher_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_aggregate_audit_researcher",
        table_name="research_aggregate_query_audit",
    )
    op.drop_table("research_aggregate_query_audit")
