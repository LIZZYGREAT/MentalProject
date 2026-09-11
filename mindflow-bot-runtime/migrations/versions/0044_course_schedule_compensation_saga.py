"""Separate course-import compensation effects from the create ledger.

Revision ID: 0044_course_schedule_compensation_saga
Revises: 0043_course_schedule_completion_presentation
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import uuid


revision = "0044_course_schedule_compensation_saga"
down_revision = "0043_course_schedule_completion_presentation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "course_schedule_imports",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "course_schedule_imports",
        sa.Column("cancel_mode", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "course_schedule_imports",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "course_schedule_imports",
        sa.Column("cleanup_error_code", sa.String(length=128), nullable=True),
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
        "'succeeded','partial_failed','cancelling','cleanup_failed','cancelled','expired')",
    )
    op.create_check_constraint(
        "ck_course_schedule_import_cancel_mode",
        "course_schedule_imports",
        "cancel_mode IS NULL OR cancel_mode IN "
        "('before_write','running_cancel','revert')",
    )
    op.drop_constraint(
        "ck_course_schedule_import_strategy_required_after_start",
        "course_schedule_imports",
        type_="check",
    )
    op.create_check_constraint(
        "ck_course_schedule_import_strategy_required_after_start",
        "course_schedule_imports",
        "status NOT IN ('running','partial_failed','succeeded','cancelling','cleanup_failed') "
        "OR recurrence_strategy IS NOT NULL",
    )

    # 0040 temporarily put deletion lifecycle in the create ledger. Preserve
    # those known identities in the new table before normalizing the old rows.
    op.create_table(
        "course_schedule_import_compensations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("write_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("provider_event_id", sa.String(length=256), nullable=False),
        sa.Column("provider_identity_kind", sa.String(length=16), nullable=False),
        sa.Column("affected_dates_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delete_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "provider_identity_kind IN ('primary','conflict')",
            name="ck_course_schedule_compensation_identity_kind",
        ),
        sa.CheckConstraint(
            "status IN ('delete_pending','deleting','deleted','delete_failed',"
            "'delete_outcome_unknown')",
            name="ck_course_schedule_compensation_status",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"], ["course_schedule_imports.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["write_id"], ["course_schedule_import_writes.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "write_id", "provider_event_id",
            name="uq_course_schedule_compensation_write_provider",
        ),
    )
    op.create_index(
        "ix_course_schedule_compensation_import_status",
        "course_schedule_import_compensations",
        ["import_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_course_schedule_compensation_participant_status",
        "course_schedule_import_compensations",
        ["participant_id", "status", "updated_at"],
    )
    bind = op.get_bind()
    legacy_rows = bind.execute(
        sa.text(
            "SELECT i.id AS import_id, w.id AS write_id, i.participant_id, "
            "w.provider_event_id, w.provider_conflict_event_id, "
            "w.affected_dates_json, w.status, w.error_code "
            "FROM course_schedule_import_writes w "
            "JOIN course_schedule_imports i ON i.id = w.import_id "
            "WHERE w.status IN ('delete_pending','deleting','deleted','delete_failed',"
            "'delete_outcome_unknown')"
        )
    ).mappings()
    insert_target = sa.text(
        "INSERT INTO course_schedule_import_compensations "
        "(id, import_id, write_id, participant_id, provider_event_id, "
        "provider_identity_kind, affected_dates_json, status, error_code, "
        "attempt_count, created_at, updated_at) VALUES "
        "(:id, :import_id, :write_id, :participant_id, :provider_event_id, "
        ":provider_identity_kind, :affected_dates_json, :status, :error_code, "
        "0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ).bindparams(
        sa.bindparam(
            "affected_dates_json",
            type_=postgresql.JSONB(),
        )
    )
    for legacy in legacy_rows:
        for kind, provider_id in (
            ("primary", legacy["provider_event_id"]),
            ("conflict", legacy["provider_conflict_event_id"]),
        ):
            if not provider_id:
                continue
            status = {
                "deleted": "deleted",
                "delete_failed": "delete_failed",
                "delete_outcome_unknown": "delete_outcome_unknown",
            }.get(legacy["status"], "delete_pending")
            bind.execute(
                insert_target,
                {
                    "id": uuid.uuid4(),
                    "import_id": legacy["import_id"],
                    "write_id": legacy["write_id"],
                    "participant_id": legacy["participant_id"],
                    "provider_event_id": provider_id,
                    "provider_identity_kind": kind,
                    "affected_dates_json": legacy["affected_dates_json"],
                    "status": status,
                    "error_code": legacy["error_code"],
                },
            )
    op.drop_constraint(
        "ck_course_schedule_write_status",
        "course_schedule_import_writes",
        type_="check",
    )
    op.execute(
        "UPDATE course_schedule_import_writes SET status = 'created' "
        "WHERE status IN ('delete_pending','deleting','deleted','delete_failed',"
        "'delete_outcome_unknown')"
    )
    op.create_check_constraint(
        "ck_course_schedule_write_status",
        "course_schedule_import_writes",
        "status IN ('planned','creating','created','create_failed',"
        "'create_outcome_unknown','create_identity_conflict','create_cancelled')",
    )
    op.execute(
        "UPDATE course_schedule_imports AS i SET status = 'cleanup_failed', "
        "cleanup_error_code = 'calendar_delete_failed' WHERE EXISTS ("
        "SELECT 1 FROM course_schedule_import_compensations c "
        "WHERE c.import_id = i.id AND c.status = 'delete_failed')"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_course_schedule_compensation_participant_status",
        table_name="course_schedule_import_compensations",
    )
    op.drop_index(
        "ix_course_schedule_compensation_import_status",
        table_name="course_schedule_import_compensations",
    )
    op.drop_table("course_schedule_import_compensations")
    op.drop_constraint(
        "ck_course_schedule_write_status",
        "course_schedule_import_writes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_course_schedule_write_status",
        "course_schedule_import_writes",
        "status IN ('planned','creating','created','create_failed',"
        "'create_outcome_unknown','create_identity_conflict','delete_pending',"
        "'deleting','deleted','delete_failed','delete_outcome_unknown')",
    )
    op.drop_constraint(
        "ck_course_schedule_import_cancel_mode",
        "course_schedule_imports",
        type_="check",
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
    op.drop_constraint(
        "ck_course_schedule_import_strategy_required_after_start",
        "course_schedule_imports",
        type_="check",
    )
    op.create_check_constraint(
        "ck_course_schedule_import_strategy_required_after_start",
        "course_schedule_imports",
        "status NOT IN ('running','partial_failed','succeeded') "
        "OR recurrence_strategy IS NOT NULL",
    )
    op.drop_column("course_schedule_imports", "cleanup_error_code")
    op.drop_column("course_schedule_imports", "cancelled_at")
    op.drop_column("course_schedule_imports", "cancel_mode")
    op.drop_column("course_schedule_imports", "cancel_requested_at")
