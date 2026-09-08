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
