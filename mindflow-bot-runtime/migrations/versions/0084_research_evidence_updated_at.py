"""Persist source-specific update timestamps for research evidence."""

from alembic import op
import sqlalchemy as sa


revision = "0084_research_evidence_updated_at"
down_revision = "0083_public_research_and_brief_topics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_evidence",
        sa.Column("updated_at", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_evidence", "updated_at")
