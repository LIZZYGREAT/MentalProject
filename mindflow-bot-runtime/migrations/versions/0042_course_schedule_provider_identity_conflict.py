"""Persist conflicting provider identities for one course schedule write.

Revision ID: 0042_course_schedule_provider_identity_conflict
Revises: 0041_course_schedule_created_provider_id_guard
"""

from alembic import op
import sqlalchemy as sa


revision = "0042_course_schedule_provider_identity_conflict"
down_revision = "0041_course_schedule_created_provider_id_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "course_schedule_import_writes",
        sa.Column("provider_conflict_event_id", sa.String(length=256), nullable=True),
    )
    op.drop_constraint(
        "ck_course_schedule_write_status",
        "course_schedule_import_writes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_course_schedule_write_status",
        "course_schedule_import_writes",
        "status IN ('planned','creating','created','create_failed',"
        "'create_outcome_unknown','create_identity_conflict',"
        "'delete_pending','deleting','deleted','delete_failed',"
        "'delete_outcome_unknown')",
    )
    op.create_check_constraint(
        "ck_course_schedule_write_conflict_provider_ids",
        "course_schedule_import_writes",
        "status <> 'create_identity_conflict' OR ("
        "provider_event_id IS NOT NULL AND trim(provider_event_id) <> '' "
        "AND provider_conflict_event_id IS NOT NULL "
        "AND trim(provider_conflict_event_id) <> '')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE course_schedule_import_writes "
        "SET status = 'create_outcome_unknown', "
        "error_code = COALESCE(NULLIF(trim(error_code), ''), "
        "'provider_identity_conflict_downgraded') "
        "WHERE status = 'create_identity_conflict'"
    )
    op.drop_constraint(
        "ck_course_schedule_write_conflict_provider_ids",
        "course_schedule_import_writes",
        type_="check",
    )
    op.drop_constraint(
        "ck_course_schedule_write_status",
        "course_schedule_import_writes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_course_schedule_write_status",
        "course_schedule_import_writes",
        "status IN ('planned','creating','created','create_failed',"
        "'create_outcome_unknown','delete_pending','deleting','deleted',"
        "'delete_failed','delete_outcome_unknown')",
    )
    op.drop_column(
        "course_schedule_import_writes", "provider_conflict_event_id"
    )
