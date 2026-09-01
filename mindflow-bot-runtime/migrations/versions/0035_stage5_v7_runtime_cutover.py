"""Require Dataset v7 for Stage-5 production eligibility.

Revision ID: 0035_stage5_v7_runtime_cutover
Revises: 0034_dataset_v7_active_history
"""

from alembic import op


revision = "0035_stage5_v7_runtime_cutover"
down_revision = "0034_dataset_v7_active_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep every historical run/profile, but revoke production eligibility for
    # Stage-5 promotions whose immutable Dataset predates frozen Current
    # Personalized / active-variant history in Schema v7.
    op.execute(
        "UPDATE parameter_learning_runs AS runs SET status = 'rejected' "
        "FROM dataset_snapshots AS snapshots "
        "WHERE runs.dataset_snapshot_id = snapshots.id "
        "AND runs.model_family = 'hierarchical-ctssm-residual.v2' "
        "AND runs.status = 'promoted' "
        "AND snapshots.schema_version <> 'mindflow-research-dataset-v7'"
    )
    op.execute(
        "UPDATE learned_model_profiles AS profiles "
        "SET validation_status = 'rejected' "
        "WHERE profiles.model_version = 'mindflow-ctssm-runtime-v11' "
        "AND profiles.validation_status = 'validated' "
        "AND profiles.parameters_json -> 'model_selection' ->> 'status' "
        "= 'stage5_promoted' "
        "AND EXISTS ("
        "SELECT 1 FROM parameter_learning_runs AS runs "
        "JOIN dataset_snapshots AS snapshots "
        "ON snapshots.id = runs.dataset_snapshot_id "
        "WHERE CAST(runs.id AS TEXT) = profiles.parameters_json "
        "-> 'model_selection' ->> 'parameter_learning_run_id' "
        "AND snapshots.schema_version <> 'mindflow-research-dataset-v7')"
    )


def downgrade() -> None:
    # Revoked eligibility cannot be reconstructed safely and is not restored.
    pass
