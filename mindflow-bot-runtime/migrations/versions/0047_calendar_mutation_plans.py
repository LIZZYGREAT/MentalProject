"""Persist participant-bound batch Calendar confirmation plans.

Revision ID: 0047_calendar_mutation_plans
Revises: 0046_course_schedule_image_sessions
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0047_calendar_mutation_plans"
down_revision = "0046_course_schedule_image_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_mutation_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column(
            "items_json",
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "operation IN ('create','delete')",
            name="ck_calendar_mutation_plan_operation",
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_confirmation','processing','succeeded',"
            "'partial_failed','cancelled','expired')",
            name="ck_calendar_mutation_plan_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calendar_mutation_plan_participant_created",
        "calendar_mutation_plans",
        ["participant_id", "created_at"],
    )
    op.create_index(
        "ix_calendar_mutation_plan_expiry",
        "calendar_mutation_plans",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_mutation_plan_expiry",
        table_name="calendar_mutation_plans",
    )
    op.drop_index(
        "ix_calendar_mutation_plan_participant_created",
        table_name="calendar_mutation_plans",
    )
    op.drop_table("calendar_mutation_plans")
