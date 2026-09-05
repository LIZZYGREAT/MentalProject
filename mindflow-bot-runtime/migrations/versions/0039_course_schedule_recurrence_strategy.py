"""Persist the confirmed course schedule recurrence strategy.

Revision ID: 0039_course_schedule_recurrence_strategy
Revises: 0038_course_schedule_import
"""

from alembic import op
import sqlalchemy as sa


revision = "0039_course_schedule_recurrence_strategy"
down_revision = "0038_course_schedule_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "course_schedule_imports",
        sa.Column("recurrence_strategy", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "course_schedule_imports",
        sa.Column("recurrence_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(sa.text(
        "UPDATE course_schedule_imports "
        "SET recurrence_strategy = 'preserve_schedule_pattern', "
        "recurrence_confirmed_at = COALESCE(confirmed_at, created_at) "
        "WHERE confirmed_at IS NOT NULL "
        "OR status IN ('running', 'partial_failed', 'succeeded')"
    ))
    op.create_check_constraint(
        "ck_course_schedule_import_recurrence_strategy",
        "course_schedule_imports",
        "recurrence_strategy IS NULL OR recurrence_strategy IN "
        "('preserve_schedule_pattern','expand_all_occurrences')",
    )
    op.create_check_constraint(
        "ck_course_schedule_import_strategy_required_after_start",
        "course_schedule_imports",
        "status NOT IN ('running','partial_failed','succeeded') "
        "OR recurrence_strategy IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_course_schedule_import_strategy_required_after_start",
        "course_schedule_imports",
        type_="check",
    )
    op.drop_constraint(
        "ck_course_schedule_import_recurrence_strategy",
        "course_schedule_imports",
        type_="check",
    )
    op.drop_column("course_schedule_imports", "recurrence_confirmed_at")
    op.drop_column("course_schedule_imports", "recurrence_strategy")
