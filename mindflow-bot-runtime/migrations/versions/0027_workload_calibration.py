"""Persist event workload calibration features and residuals.

Revision ID: 0027_workload_calibration
Revises: 0026_dataset_participant_membership
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0027_workload_calibration"
down_revision = "0026_dataset_participant_membership"
branch_labels = None
depends_on = None


JSON_VALUE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("event_appraisal_feedback", sa.Column("event_type", sa.String(32), nullable=True))
    op.add_column("event_appraisal_feedback", sa.Column("course_name", sa.String(200), nullable=True))
    op.add_column("event_appraisal_feedback", sa.Column("workload_feature_vector", JSON_VALUE, nullable=True))
    op.add_column("event_appraisal_feedback", sa.Column("workload_prior", sa.Float(), nullable=True))
    op.add_column("event_appraisal_feedback", sa.Column("observed_workload", sa.Float(), nullable=True))
    op.add_column("event_appraisal_feedback", sa.Column("workload_residual", sa.Float(), nullable=True))
    op.add_column("event_appraisal_feedback", sa.Column("workload_model_version", sa.String(64), nullable=True))
    op.create_check_constraint(
        "ck_event_appraisal_workload_prior",
        "event_appraisal_feedback",
        "workload_prior IS NULL OR (workload_prior >= 0 AND workload_prior <= 1)",
    )
    op.create_check_constraint(
        "ck_event_appraisal_observed_workload",
        "event_appraisal_feedback",
        "observed_workload IS NULL OR (observed_workload >= 0 AND observed_workload <= 1)",
    )
    op.create_index(
        "ix_event_appraisal_event_type",
        "event_appraisal_feedback",
        ["event_type", "submitted_at"],
    )
    op.create_index(
        "ix_event_appraisal_course",
        "event_appraisal_feedback",
        ["course_name", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_event_appraisal_course", table_name="event_appraisal_feedback")
    op.drop_index("ix_event_appraisal_event_type", table_name="event_appraisal_feedback")
    op.drop_constraint("ck_event_appraisal_observed_workload", "event_appraisal_feedback", type_="check")
    op.drop_constraint("ck_event_appraisal_workload_prior", "event_appraisal_feedback", type_="check")
    for name in (
        "workload_model_version",
        "workload_residual",
        "observed_workload",
        "workload_prior",
        "workload_feature_vector",
        "course_name",
        "event_type",
    ):
        op.drop_column("event_appraisal_feedback", name)
