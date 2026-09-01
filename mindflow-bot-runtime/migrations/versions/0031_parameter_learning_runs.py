"""Add Stage-5 hierarchical parameter learning runs.

Revision ID: 0031_parameter_learning_runs
Revises: 0030_model_promotion_decisions
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0031_parameter_learning_runs"
down_revision = "0030_model_promotion_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "parameter_learning_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("model_family", sa.String(length=64), nullable=False),
        sa.Column("parameters_before", jsonb, nullable=False),
        sa.Column("parameters_candidate", jsonb, nullable=False),
        sa.Column("training_metrics", jsonb, nullable=False),
        sa.Column("validation_metrics", jsonb, nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('candidate', 'rejected', 'promoted')",
            name="ck_parameter_learning_status",
        ),
        sa.CheckConstraint(
            "sample_count >= 0",
            name="ck_parameter_learning_sample_count",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"], ["dataset_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_parameter_learning_participant_created",
        "parameter_learning_runs",
        ["participant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_parameter_learning_snapshot_created",
        "parameter_learning_runs",
        ["dataset_snapshot_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_parameter_learning_snapshot_created",
        table_name="parameter_learning_runs",
    )
    op.drop_index(
        "ix_parameter_learning_participant_created",
        table_name="parameter_learning_runs",
    )
    op.drop_table("parameter_learning_runs")
