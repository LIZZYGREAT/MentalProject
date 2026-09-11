"""Queue course schedule imports and persist one row per Calendar write.

Revision ID: 0040_course_schedule_import_ledger
Revises: 0039_course_schedule_recurrence_strategy
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0040_course_schedule_import_ledger"
down_revision = "0039_course_schedule_recurrence_strategy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "course_schedule_imports",
        sa.Column("run_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "course_schedule_imports",
        sa.Column("status_card_message_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "course_schedule_imports",
        sa.Column("status_card_chat_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "course_schedule_imports",
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_course_schedule_import_status",
        "course_schedule_imports",
        type_="check",
    )
    op.create_check_constraint(
        "ck_course_schedule_import_status",
        "course_schedule_imports",
        "status IN ('pending_context','pending_confirmation','queued','running',"
        "'succeeded','partial_failed','cancelled','expired')",
    )
    op.create_table(
        "course_schedule_import_writes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_identity", sa.String(length=128), nullable=False),
        sa.Column("source_identity", sa.String(length=512), nullable=False),
        sa.Column("write_kind", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recurrence", sa.String(length=1024), nullable=True),
        sa.Column("affected_dates_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_event_id", sa.String(length=256), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('planned','creating','created','create_failed',"
            "'create_outcome_unknown','delete_pending','deleting','deleted',"
            "'delete_failed','delete_outcome_unknown')",
            name="ck_course_schedule_write_status",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"], ["course_schedule_imports.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["course_schedule_import_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "import_id", "item_id", "occurrence_identity",
            name="uq_course_schedule_write_occurrence",
        ),
        sa.UniqueConstraint("source_identity", name="uq_course_schedule_write_source"),
    )
    op.create_index(
        "ix_course_schedule_write_import_status",
        "course_schedule_import_writes",
        ["import_id", "status", "created_at"],
    )
    op.create_index(
        "ix_course_schedule_write_item_status",
        "course_schedule_import_writes",
        ["item_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_course_schedule_write_item_status",
        table_name="course_schedule_import_writes",
    )
    op.drop_index(
        "ix_course_schedule_write_import_status",
        table_name="course_schedule_import_writes",
    )
    op.drop_table("course_schedule_import_writes")
    op.drop_constraint(
        "ck_course_schedule_import_status",
        "course_schedule_imports",
        type_="check",
    )
    op.create_check_constraint(
        "ck_course_schedule_import_status",
        "course_schedule_imports",
        "status IN ('pending_context','pending_confirmation','running','succeeded',"
        "'partial_failed','cancelled','expired')",
    )
    op.drop_column("course_schedule_imports", "last_progress_at")
    op.drop_column("course_schedule_imports", "status_card_chat_id")
    op.drop_column("course_schedule_imports", "status_card_message_id")
    op.drop_column("course_schedule_imports", "run_requested_at")
