"""Add scoped researcher access metadata.

Revision ID: 0061_research_access_scope
Revises: 0060_personalization_domains
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0061_research_access_scope"
down_revision = "0060_personalization_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "participants",
        sa.Column(
            "access_tier", sa.String(length=24),
            nullable=False, server_default="participant",
        ),
    )
    op.add_column(
        "participants",
        sa.Column(
            "scopes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_participant_access_tier",
        "participants",
        "access_tier IN ('participant', 'researcher')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_participant_access_tier", "participants", type_="check"
    )
    op.drop_column("participants", "scopes_json")
    op.drop_column("participants", "access_tier")
