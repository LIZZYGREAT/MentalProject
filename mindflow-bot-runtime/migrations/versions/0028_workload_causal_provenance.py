"""Freeze point-in-time workload provenance for event appraisals.

Revision ID: 0028_workload_causal_provenance
Revises: 0027_workload_calibration
"""

from alembic import op
import sqlalchemy as sa


revision = "0028_workload_causal_provenance"
down_revision = "0027_workload_calibration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event_appraisal_feedback", sa.Column("event_local_date", sa.Date(), nullable=True))
    op.add_column(
        "event_appraisal_feedback",
        sa.Column("event_start_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "event_appraisal_feedback",
        sa.Column("source_forecast_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "event_appraisal_feedback",
        sa.Column("source_forecast_version", sa.String(64), nullable=True),
    )
    op.add_column(
        "event_appraisal_feedback",
        sa.Column("source_semantic_revision", sa.String(64), nullable=True),
    )
    op.add_column(
        "event_appraisal_feedback",
        sa.Column("workload_schema_version", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_event_appraisal_source_forecast",
        "event_appraisal_feedback",
        "forecast_snapshots",
        ["source_forecast_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_event_appraisal_participant_event_date",
        "event_appraisal_feedback",
        ["participant_id", "event_local_date"],
    )
    op.create_index(
        "ix_event_appraisal_source_forecast",
        "event_appraisal_feedback",
        ["source_forecast_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_event_appraisal_source_forecast", table_name="event_appraisal_feedback")
    op.drop_index(
        "ix_event_appraisal_participant_event_date",
        table_name="event_appraisal_feedback",
    )
    op.drop_constraint(
        "fk_event_appraisal_source_forecast",
        "event_appraisal_feedback",
        type_="foreignkey",
    )
    for name in (
        "workload_schema_version",
        "source_semantic_revision",
        "source_forecast_version",
        "source_forecast_id",
        "event_start_at",
        "event_local_date",
    ):
        op.drop_column("event_appraisal_feedback", name)
