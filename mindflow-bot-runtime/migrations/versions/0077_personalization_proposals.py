"""Add typed personalization review proposals.

Revision ID: 0077_personalization_proposals
Revises: 0076_reminder_proposals
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0077_personalization_proposals"
down_revision = "0076_reminder_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personalization_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "result_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "domain IN ('memory', 'interaction_preferences', 'support_preferences')",
            name="ck_personalization_proposal_domain",
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_confirmation', 'executing', 'confirmed', "
            "'cancelled', 'expired', 'failed')",
            name="ck_personalization_proposal_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_personalization_proposal_participant_created",
        "personalization_proposals",
        ["participant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_personalization_proposal_expiry",
        "personalization_proposals",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_personalization_proposal_expiry",
        table_name="personalization_proposals",
    )
    op.drop_index(
        "ix_personalization_proposal_participant_created",
        table_name="personalization_proposals",
    )
    op.drop_table("personalization_proposals")
