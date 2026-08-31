"""Add durable Stage-4 model promotion provenance.

Revision ID: 0030_model_promotion_decisions
Revises: 0029_ctssm_vnext_recovery_snapshot
"""

from alembic import op
import sqlalchemy as sa


revision = "0030_model_promotion_decisions"
down_revision = "0029_ctssm_vnext_recovery_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_promotion_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=True),
        sa.Column("model_family", sa.String(length=32), nullable=False),
        sa.Column("promotion_gate_version", sa.String(length=64), nullable=False),
        sa.Column("evaluation_code_version", sa.String(length=64), nullable=False),
        sa.Column("parameters_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("passed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status = 'retained_from_empirical_evidence'",
            name="ck_model_promotion_status",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"], ["dataset_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["model_evaluation_run_id"], ["model_evaluation_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_evaluation_run_id",
            "participant_id",
            "model_family",
            name="uq_model_promotion_run_participant_family",
        ),
    )
    op.create_index(
        "ix_model_promotion_participant_promoted",
        "model_promotion_decisions",
        ["participant_id", "promoted_at"],
        unique=False,
    )
    op.create_index(
        "uq_model_promotion_cohort_run_family",
        "model_promotion_decisions",
        ["model_evaluation_run_id", "model_family"],
        unique=True,
        postgresql_where=sa.text("participant_id IS NULL"),
        sqlite_where=sa.text("participant_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_model_promotion_cohort_run_family",
        table_name="model_promotion_decisions",
    )
    op.drop_index(
        "ix_model_promotion_participant_promoted",
        table_name="model_promotion_decisions",
    )
    op.drop_table("model_promotion_decisions")
