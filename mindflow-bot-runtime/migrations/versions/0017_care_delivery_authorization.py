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
        "calendar_snapshots",
        sa.Column(
            "snapshot_state",
            sa.String(length=32),
            nullable=False,
            server_default="current",
        ),
    )
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
    op.execute(
        """
        UPDATE calendar_snapshots
        SET snapshot_state = 'provider_degraded'
        WHERE degraded = true
        """
    )
    # 0016 stored snooze provenance in JSON. Join on text so malformed or
    # missing historical values remain NULL instead of aborting the migration.
    op.execute(
        """
        WITH snooze_candidates AS (
            SELECT
                warning.id AS warning_id,
                intervention.id AS intervention_id,
                ROW_NUMBER() OVER (
                    PARTITION BY intervention.id
                    ORDER BY warning.updated_at ASC, warning.id ASC
                ) AS candidate_rank
            FROM warning_schedules AS warning
            JOIN care_intervention_events AS intervention
              ON warning.payload_json ->> 'snoozed_from_intervention_id'
                 = intervention.id::text
            WHERE warning.snoozed_from_intervention_id IS NULL
        )
        UPDATE warning_schedules AS warning
        SET snoozed_from_intervention_id = candidate.intervention_id
        FROM snooze_candidates AS candidate
        WHERE warning.id = candidate.warning_id
          AND candidate.candidate_rank = 1
        """
    )
    op.alter_column(
        "calendar_snapshots", "snapshot_state", server_default=None
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
    op.drop_column("calendar_snapshots", "snapshot_state")
