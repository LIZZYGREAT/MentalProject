"""Make batch Calendar mutation execution crash recoverable.

Revision ID: 0048_calendar_mutation_plan_items
Revises: 0047_calendar_mutation_plans
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0048_calendar_mutation_plan_items"
down_revision = "0047_calendar_mutation_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_mutation_plans",
        sa.Column("run_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "calendar_mutation_plans",
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "calendar_mutation_plans",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "calendar_mutation_plans",
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "calendar_mutation_plans",
        sa.Column("status_card_message_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "calendar_mutation_plans",
        sa.Column("status_card_chat_id", sa.String(length=256), nullable=True),
    )
    op.drop_constraint(
        "ck_calendar_mutation_plan_status",
        "calendar_mutation_plans",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calendar_mutation_plan_status",
        "calendar_mutation_plans",
        "status IN ('awaiting_confirmation','queued','running','succeeded',"
        "'partial_failed','recovery_required','cancelled','expired')",
    )
    op.create_index(
        "ix_calendar_mutation_plan_lease",
        "calendar_mutation_plans",
        ["status", "lease_expires_at"],
    )
    op.create_table(
        "calendar_mutation_plan_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_event_id", sa.String(length=256), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_identity", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('create','delete')",
            name="ck_calendar_mutation_plan_item_operation",
        ),
        sa.CheckConstraint(
            "status IN ('pending','creating','deleting','succeeded','failed',"
            "'outcome_unknown')",
            name="ck_calendar_mutation_plan_item_status",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["calendar_mutation_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id", "item_index", name="uq_calendar_mutation_plan_item_index"
        ),
        sa.UniqueConstraint(
            "source_identity", name="uq_calendar_mutation_plan_item_source"
        ),
    )
    op.create_index(
        "ix_calendar_mutation_plan_item_status",
        "calendar_mutation_plan_items",
        ["plan_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_calendar_mutation_plan_item_status",
        table_name="calendar_mutation_plan_items",
    )
    op.drop_table("calendar_mutation_plan_items")
    op.drop_index(
        "ix_calendar_mutation_plan_lease", table_name="calendar_mutation_plans"
    )
    op.drop_constraint(
        "ck_calendar_mutation_plan_status",
        "calendar_mutation_plans",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calendar_mutation_plan_status",
        "calendar_mutation_plans",
        "status IN ('awaiting_confirmation','processing','succeeded',"
        "'partial_failed','cancelled','expired')",
    )
    for column in (
        "status_card_chat_id",
        "status_card_message_id",
        "last_progress_at",
        "lease_expires_at",
        "lease_owner",
        "run_requested_at",
    ):
        op.drop_column("calendar_mutation_plans", column)
