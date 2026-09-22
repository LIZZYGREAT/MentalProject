"""Add participant-bound reminder review proposals.

Revision ID: 0076_reminder_proposals
Revises: 0075_calendar_plan_presentation_context
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0076_reminder_proposals"
down_revision = "0075_calendar_plan_presentation_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminder_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "operation IN ('create', 'cancel')",
            name="ck_reminder_proposal_operation",
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_confirmation', 'confirmed', 'cancelled', 'expired')",
            name="ck_reminder_proposal_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reminder_proposal_participant_created",
        "reminder_proposals",
        ["participant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_reminder_proposal_expiry",
        "reminder_proposals",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reminder_proposal_expiry", table_name="reminder_proposals"
    )
    op.drop_index(
        "ix_reminder_proposal_participant_created",
        table_name="reminder_proposals",
    )
    op.drop_table("reminder_proposals")
