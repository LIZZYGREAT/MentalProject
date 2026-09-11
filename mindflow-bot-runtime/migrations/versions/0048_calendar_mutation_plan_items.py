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
    bind = op.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    # 0047 plans had no per-item ledger. A pre-existing processing plan cannot
    # be safely replayed because the provider side effect boundary is unknown;
    # fail closed and let an operator audit it before retrying the migration.
    processing_count = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM calendar_mutation_plans "
                "WHERE status = 'processing'"
            )
        ).scalar_one()
    )
    if processing_count:
        raise RuntimeError(
            "0048 refuses to upgrade while legacy processing Calendar plans "
            f"exist (count={processing_count}); audit them first"
        )

    # Legacy awaiting-confirmation plans have never been allowed to write to
    # Calendar. Expire them explicitly instead of creating empty ledgers that
    # could later be reported as a successful zero-item run.
    if dialect_name == "postgresql":
        bind.execute(
            sa.text(
                "UPDATE calendar_mutation_plans "
                "SET status = 'expired', "
                "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP, "
                "result_json = COALESCE(result_json, '{}'::jsonb) "
                "|| jsonb_build_object('upgrade_reason', "
                "'legacy_plan_expired_on_0048_upgrade') "
                "WHERE status = 'awaiting_confirmation'"
            )
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE calendar_mutation_plans "
                "SET status = 'expired', "
                "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP, "
                "result_json = COALESCE(result_json, '{}') "
                "WHERE status = 'awaiting_confirmation'"
            )
        )

    # Only after legacy rows have been made safe do we install the new status
    # constraint. The entire Alembic transaction rolls back on the guard above.
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
