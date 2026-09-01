"""Harden Stage-5 snapshot causality and scheduled-run idempotency.

Revision ID: 0032_stage5_causal_hardening
Revises: 0031_parameter_learning_runs
"""

from alembic import op
import sqlalchemy as sa


revision = "0032_stage5_causal_hardening"
down_revision = "0031_parameter_learning_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        "item_type IN ('participant', 'observation', 'forecast', "
        "'forecast_currentness', 'calendar', 'match_source', "
        "'psychometric', 'daily_review', 'slow_state', "
        "'care_intervention_exposure', 'warning_delivery')",
    )
    op.add_column(
        "parameter_learning_runs",
        sa.Column(
            "run_kind",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
    )
    op.add_column(
        "parameter_learning_runs",
        sa.Column("schedule_key", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "ck_parameter_learning_run_kind",
        "parameter_learning_runs",
        "run_kind IN ('manual', 'scheduled')",
    )
    op.create_check_constraint(
        "ck_parameter_learning_schedule_key",
        "parameter_learning_runs",
        "(run_kind = 'manual' AND schedule_key IS NULL) OR "
        "(run_kind = 'scheduled' AND schedule_key IS NOT NULL)",
    )
    op.create_index(
        "uq_parameter_learning_scheduled_week",
        "parameter_learning_runs",
        ["participant_id", "model_family", "schedule_key"],
        unique=True,
        postgresql_where=sa.text("run_kind = 'scheduled'"),
        sqlite_where=sa.text("run_kind = 'scheduled'"),
    )

    # v1 conflated beta_R and kappa_down.  No evidence-preserving split is
    # possible, so old Stage-5 candidates/actives fail closed and must retrain.
    op.execute(
        "UPDATE parameter_learning_runs SET status = 'rejected' "
        "WHERE model_family = 'hierarchical-ctssm-residual.v1' "
        "AND status IN ('candidate', 'promoted')"
    )
    op.execute(
        "UPDATE learned_model_profiles SET validation_status = 'rejected' "
        "WHERE model_version = 'mindflow-ctssm-runtime-v9' "
        "AND validation_status IN ('candidate', 'validated')"
    )


def downgrade() -> None:
    op.drop_index(
        "uq_parameter_learning_scheduled_week",
        table_name="parameter_learning_runs",
    )
    op.drop_constraint(
        "ck_parameter_learning_schedule_key",
        "parameter_learning_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_parameter_learning_run_kind",
        "parameter_learning_runs",
        type_="check",
    )
    op.drop_column("parameter_learning_runs", "schedule_key")
    op.drop_column("parameter_learning_runs", "run_kind")
    op.drop_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        type_="check",
    )
    op.execute(
        "DELETE FROM dataset_snapshot_items WHERE item_type IN "
        "('care_intervention_exposure', 'warning_delivery')"
    )
    op.create_check_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        "item_type IN ('participant', 'observation', 'forecast', "
        "'forecast_currentness', 'calendar', 'match_source', "
        "'psychometric', 'daily_review', 'slow_state')",
    )
