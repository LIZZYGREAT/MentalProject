"""Add the stage-1 research contract and four-layer profile schema.

Revision ID: 0022_research_profile_v2
Revises: 0021_daily_review_energy_optional
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0022_research_profile_v2"
down_revision = "0021_daily_review_energy_optional"
branch_labels = None
depends_on = None


def _score_constraint(column: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} >= 0 AND {column} <= 10",
        name=name,
    )


def upgrade() -> None:
    op.create_table(
        "psychometric_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_name", sa.String(length=64), nullable=False),
        sa.Column("instrument_version", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("raw_items_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scores_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("administered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_period", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_psychometric_participant_time",
        "psychometric_assessments",
        ["participant_id", "administered_at"],
        unique=False,
    )

    op.create_table(
        "participant_slow_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cadence", sa.String(length=16), nullable=False),
        sa.Column("rolling_7d_stress", sa.Float(), nullable=True),
        sa.Column("rolling_7d_workload", sa.Float(), nullable=True),
        sa.Column("rolling_7d_energy", sa.Float(), nullable=True),
        sa.Column("recent_recovery_quality", sa.Float(), nullable=True),
        sa.Column("recent_sleep_debt", sa.Float(), nullable=True),
        sa.Column("exam_period_flag", sa.Boolean(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_slow_state_participant_time",
        "participant_slow_states",
        ["participant_id", "effective_at"],
        unique=False,
    )

    op.create_table(
        "event_appraisal_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(length=256), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("mental_demand", sa.Float(), nullable=False),
        sa.Column("physical_demand", sa.Float(), nullable=False),
        sa.Column("temporal_demand", sa.Float(), nullable=False),
        sa.Column("effort", sa.Float(), nullable=False),
        sa.Column("frustration", sa.Float(), nullable=False),
        sa.Column("perceived_control", sa.Float(), nullable=False),
        sa.Column("actual_stress", sa.Float(), nullable=False),
        sa.Column("perceived_performance", sa.Float(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        _score_constraint("mental_demand", "ck_event_appraisal_mental"),
        _score_constraint("physical_demand", "ck_event_appraisal_physical"),
        _score_constraint("temporal_demand", "ck_event_appraisal_temporal"),
        _score_constraint("effort", "ck_event_appraisal_effort"),
        _score_constraint("frustration", "ck_event_appraisal_frustration"),
        _score_constraint("perceived_control", "ck_event_appraisal_control"),
        _score_constraint("actual_stress", "ck_event_appraisal_stress"),
        _score_constraint("perceived_performance", "ck_event_appraisal_performance"),
    )
    op.create_index(
        "ix_event_appraisal_participant_time",
        "event_appraisal_feedback",
        ["participant_id", "submitted_at"],
        unique=False,
    )

    op.add_column(
        "learned_model_profiles",
        sa.Column(
            "uncertainty_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "learned_model_profiles",
        sa.Column(
            "model_version",
            sa.String(length=64),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.add_column(
        "learned_model_profiles",
        sa.Column(
            "validation_status",
            sa.String(length=32),
            server_default="candidate",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("learned_model_profiles", "validation_status")
    op.drop_column("learned_model_profiles", "model_version")
    op.drop_column("learned_model_profiles", "uncertainty_json")
    op.drop_index("ix_event_appraisal_participant_time", table_name="event_appraisal_feedback")
    op.drop_table("event_appraisal_feedback")
    op.drop_index("ix_slow_state_participant_time", table_name="participant_slow_states")
    op.drop_table("participant_slow_states")
    op.drop_index("ix_psychometric_participant_time", table_name="psychometric_assessments")
    op.drop_table("psychometric_assessments")

