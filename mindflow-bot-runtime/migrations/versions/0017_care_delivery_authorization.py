"""Harden care snooze identity and delivery authorization.

Revision ID: 0017_care_delivery_authorization
Revises: 0016_care_intervention_feedback
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_care_delivery_authorization"
down_revision = "0016_care_intervention_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "warning_schedules",
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "daily_review_schedules",
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "warning_schedules",
        sa.Column("snoozed_from_intervention_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_warning_snoozed_from_intervention",
        "warning_schedules",
        "care_intervention_events",
        ["snoozed_from_intervention_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_warning_snoozed_from_intervention",
        "warning_schedules",
        ["snoozed_from_intervention_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_warning_snoozed_from_intervention",
        "warning_schedules",
        type_="unique",
    )
    op.drop_constraint(
        "fk_warning_snoozed_from_intervention",
        "warning_schedules",
        type_="foreignkey",
    )
    op.drop_column("warning_schedules", "snoozed_from_intervention_id")
    op.drop_column("daily_review_schedules", "authorized_at")
    op.drop_column("warning_schedules", "authorized_at")
