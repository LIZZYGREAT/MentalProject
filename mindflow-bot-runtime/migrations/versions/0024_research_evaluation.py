"""Add Stage-2 research evaluation persistence.

Revision ID: 0024_research_evaluation
Revises: 0023_stage1_gate_constraints
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0024_research_evaluation"
down_revision = "0023_stage1_gate_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "forecast_observation_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("forecast_id", sa.Uuid(), nullable=False),
        sa.Column("forecast_version", sa.String(length=64), nullable=False),
        sa.Column("forecast_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_stress", sa.Float(), nullable=False),
        sa.Column("actual_stress", sa.Float(), nullable=False),
        sa.Column("residual", sa.Float(), nullable=False),
        sa.Column("prediction_lower", sa.Float(), nullable=True),
        sa.Column("prediction_upper", sa.Float(), nullable=True),
        sa.Column("context_json", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "actual_stress >= 0 AND actual_stress <= 10",
            name="ck_forecast_match_actual_stress",
        ),
        sa.CheckConstraint(
            "predicted_stress >= 0 AND predicted_stress <= 10",
            name="ck_forecast_match_predicted_stress",
        ),
        sa.CheckConstraint(
            "prediction_lower IS NULL OR "
            "(prediction_lower >= 0 AND prediction_lower <= 10)",
            name="ck_forecast_match_prediction_lower",
        ),
        sa.CheckConstraint(
            "prediction_upper IS NULL OR "
            "(prediction_upper >= 0 AND prediction_upper <= 10)",
            name="ck_forecast_match_prediction_upper",
        ),
        sa.CheckConstraint(
            "prediction_lower IS NULL OR prediction_upper IS NULL OR "
            "prediction_lower <= prediction_upper",
            name="ck_forecast_match_interval_order",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["forecast_id"], ["forecast_snapshots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["state_observations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_id", "forecast_id", name="uq_forecast_observation_match"
        ),
    )
    op.create_index(
        "ix_forecast_observation_match_participant_day",
        "forecast_observation_matches",
        ["participant_id", "local_date", "observed_at"],
        unique=False,
    )

    op.create_table(
        "dataset_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_start", sa.Date(), nullable=False),
        sa.Column("date_end", sa.Date(), nullable=False),
        sa.Column("participant_filter", jsonb, nullable=False),
        sa.Column("observation_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calendar_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", jsonb, nullable=False),
        sa.CheckConstraint("date_start <= date_end", name="ck_dataset_snapshot_dates"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dataset_snapshot_created",
        "dataset_snapshots",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "model_evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=True),
        sa.Column("metrics_json", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_model_evaluation_status",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"], ["dataset_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_evaluation_snapshot_created",
        "model_evaluation_runs",
        ["dataset_snapshot_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_model_evaluation_model_created",
        "model_evaluation_runs",
        ["model_version", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_evaluation_model_created", table_name="model_evaluation_runs"
    )
    op.drop_index(
        "ix_model_evaluation_snapshot_created", table_name="model_evaluation_runs"
    )
    op.drop_table("model_evaluation_runs")
    op.drop_index("ix_dataset_snapshot_created", table_name="dataset_snapshots")
    op.drop_table("dataset_snapshots")
    op.drop_index(
        "ix_forecast_observation_match_participant_day",
        table_name="forecast_observation_matches",
    )
    op.drop_table("forecast_observation_matches")
