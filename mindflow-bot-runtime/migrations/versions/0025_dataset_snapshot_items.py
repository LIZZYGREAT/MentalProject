"""Freeze Stage-2 dataset sources and evaluation semantics.

Revision ID: 0025_dataset_snapshot_items
Revises: 0024_research_evaluation
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0025_dataset_snapshot_items"
down_revision = "0024_research_evaluation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "forecast_observation_matches",
        sa.Column(
            "match_schema_version",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE forecast_observation_matches "
        "SET match_schema_version = 'forecast-observation-grid.v1' "
        "WHERE match_schema_version IS NULL"
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY observation_id
                       ORDER BY created_at DESC, id DESC
                   ) AS position
            FROM forecast_observation_matches
        )
        DELETE FROM forecast_observation_matches AS match
        USING ranked
        WHERE match.id = ranked.id AND ranked.position > 1
        """
    )
    op.alter_column(
        "forecast_observation_matches",
        "match_schema_version",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.drop_constraint(
        "uq_forecast_observation_match",
        "forecast_observation_matches",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_forecast_observation_match_schema",
        "forecast_observation_matches",
        ["observation_id", "match_schema_version"],
    )

    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "dataset_snapshot_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_version", sa.String(length=128), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "item_type IN ('observation', 'forecast', "
            "'forecast_currentness', 'calendar', 'match_source')",
            name="ck_dataset_snapshot_item_type",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"],
            ["dataset_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_snapshot_id",
            "item_type",
            "source_id",
            "source_version",
            name="uq_dataset_snapshot_item_source",
        ),
    )
    op.create_index(
        "ix_dataset_snapshot_item_snapshot_type",
        "dataset_snapshot_items",
        ["dataset_snapshot_id", "item_type"],
        unique=False,
    )
    op.create_index(
        "ix_dataset_snapshot_item_participant_day",
        "dataset_snapshot_items",
        ["participant_id", "local_date"],
        unique=False,
    )

    op.add_column(
        "model_evaluation_runs",
        sa.Column(
            "evaluation_mode",
            sa.String(length=32),
            nullable=False,
            server_default="historical_online",
        ),
    )
    op.add_column(
        "model_evaluation_runs",
        sa.Column(
            "evaluation_code_version",
            sa.String(length=64),
            nullable=False,
            server_default="stage2-evaluation.v2",
        ),
    )
    op.drop_constraint(
        "ck_model_evaluation_status",
        "model_evaluation_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_model_evaluation_status",
        "model_evaluation_runs",
        "status IN ('pending', 'running', 'completed', 'failed', "
        "'not_implemented')",
    )
    op.create_check_constraint(
        "ck_model_evaluation_mode",
        "model_evaluation_runs",
        "evaluation_mode IN ('historical_online', 'offline_replay')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_model_evaluation_mode",
        "model_evaluation_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_model_evaluation_status",
        "model_evaluation_runs",
        type_="check",
    )
    op.execute(
        "UPDATE model_evaluation_runs SET status = 'failed' "
        "WHERE status = 'not_implemented'"
    )
    op.create_check_constraint(
        "ck_model_evaluation_status",
        "model_evaluation_runs",
        "status IN ('pending', 'running', 'completed', 'failed')",
    )
    op.drop_column("model_evaluation_runs", "evaluation_code_version")
    op.drop_column("model_evaluation_runs", "evaluation_mode")

    op.drop_index(
        "ix_dataset_snapshot_item_participant_day",
        table_name="dataset_snapshot_items",
    )
    op.drop_index(
        "ix_dataset_snapshot_item_snapshot_type",
        table_name="dataset_snapshot_items",
    )
    op.drop_table("dataset_snapshot_items")

    op.drop_constraint(
        "uq_forecast_observation_match_schema",
        "forecast_observation_matches",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_forecast_observation_match",
        "forecast_observation_matches",
        ["observation_id", "forecast_id"],
    )
    op.drop_column(
        "forecast_observation_matches", "match_schema_version"
    )
