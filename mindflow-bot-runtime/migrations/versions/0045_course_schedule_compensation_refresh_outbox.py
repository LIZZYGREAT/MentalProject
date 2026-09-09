"""Persist downstream rollback refresh work for deleted course events.

Revision ID: 0045_course_schedule_compensation_refresh_outbox
Revises: 0044_course_schedule_compensation_saga
"""

from alembic import op
import sqlalchemy as sa


revision = "0045_course_schedule_compensation_refresh_outbox"
down_revision = "0044_course_schedule_compensation_saga"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "course_schedule_import_compensations",
        sa.Column("rollback_refresh_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "course_schedule_import_compensations",
        sa.Column(
            "rollback_refresh_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "course_schedule_import_compensations",
        sa.Column(
            "rollback_refresh_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "course_schedule_import_compensations",
        sa.Column(
            "rollback_refresh_claim_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "course_schedule_import_compensations",
        sa.Column(
            "rollback_refresh_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "course_schedule_import_compensations",
        sa.Column(
            "rollback_refresh_error_code",
            sa.String(length=128),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE course_schedule_import_compensations "
        "SET rollback_refresh_status = 'pending', "
        "rollback_refresh_next_attempt_at = CURRENT_TIMESTAMP "
        "WHERE status = 'deleted' AND rollback_refresh_status IS NULL"
    )
    op.create_check_constraint(
        "ck_course_schedule_compensation_refresh_status",
        "course_schedule_import_compensations",
        "rollback_refresh_status IS NULL OR rollback_refresh_status IN "
        "('pending','processing','completed')",
    )
    op.create_check_constraint(
        "ck_course_schedule_compensation_deleted_refresh",
        "course_schedule_import_compensations",
        "status <> 'deleted' OR rollback_refresh_status IS NOT NULL",
    )
    op.create_index(
        "ix_course_schedule_compensation_refresh_due",
        "course_schedule_import_compensations",
        [
            "status",
            "rollback_refresh_status",
            "rollback_refresh_next_attempt_at",
            "rollback_refresh_claim_expires_at",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_course_schedule_compensation_refresh_due",
        table_name="course_schedule_import_compensations",
    )
    op.drop_constraint(
        "ck_course_schedule_compensation_deleted_refresh",
        "course_schedule_import_compensations",
        type_="check",
    )
    op.drop_constraint(
        "ck_course_schedule_compensation_refresh_status",
        "course_schedule_import_compensations",
        type_="check",
    )
    op.drop_column(
        "course_schedule_import_compensations",
        "rollback_refresh_error_code",
    )
    op.drop_column(
        "course_schedule_import_compensations",
        "rollback_refresh_completed_at",
    )
    op.drop_column(
        "course_schedule_import_compensations",
        "rollback_refresh_claim_expires_at",
    )
    op.drop_column(
        "course_schedule_import_compensations",
        "rollback_refresh_next_attempt_at",
    )
    op.drop_column(
        "course_schedule_import_compensations",
        "rollback_refresh_attempt_count",
    )
    op.drop_column(
        "course_schedule_import_compensations",
        "rollback_refresh_status",
    )
