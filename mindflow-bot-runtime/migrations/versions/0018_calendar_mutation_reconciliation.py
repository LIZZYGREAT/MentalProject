"""Persist Calendar mutation reconciliation work.

Revision ID: 0018_calendar_mutation_reconciliation
Revises: 0017_care_delivery_authorization
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0018_calendar_mutation_reconciliation"
down_revision = "0017_care_delivery_authorization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_mutation_reconciliations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("mutation_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "work_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="prepared",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calendar_mutation_reconciliation_due",
        "calendar_mutation_reconciliations",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_calendar_mutation_reconciliation_participant",
        "calendar_mutation_reconciliations",
        ["participant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_mutation_reconciliation_participant",
        table_name="calendar_mutation_reconciliations",
    )
    op.drop_index(
        "ix_calendar_mutation_reconciliation_due",
        table_name="calendar_mutation_reconciliations",
    )
    op.drop_table("calendar_mutation_reconciliations")
