"""Require a provider identity for created course schedule writes.

Revision ID: 0041_course_schedule_created_provider_id_guard
Revises: 0040_course_schedule_import_ledger
"""

from alembic import op


revision = "0041_course_schedule_created_provider_id_guard"
down_revision = "0040_course_schedule_import_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0040 permitted ``created`` rows without a provider identity. Such a row
    # is not proof of a committed provider event, so preserve the uncertainty
    # instead of inventing an event id before adding the invariant.
    op.execute(
        "UPDATE course_schedule_import_writes "
        "SET status = 'create_outcome_unknown', "
        "error_code = COALESCE(NULLIF(trim(error_code), ''), "
        "'legacy_missing_provider_event_id'), "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE status = 'created' AND (provider_event_id IS NULL "
        "OR trim(provider_event_id) = '')"
    )
    op.execute(
        "UPDATE course_schedule_imports AS i "
        "SET status = 'partial_failed', completed_at = NULL, "
        "last_progress_at = CURRENT_TIMESTAMP "
        "WHERE EXISTS (SELECT 1 FROM course_schedule_import_writes AS w "
        "WHERE w.import_id = i.id "
        "AND w.status = 'create_outcome_unknown' "
        "AND w.error_code = 'legacy_missing_provider_event_id')"
    )
    op.create_check_constraint(
        "ck_course_schedule_write_created_provider_id",
        "course_schedule_import_writes",
        "status <> 'created' OR (provider_event_id IS NOT NULL "
        "AND trim(provider_event_id) <> '')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_course_schedule_write_created_provider_id",
        "course_schedule_import_writes",
        type_="check",
    )
