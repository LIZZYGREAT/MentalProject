"""Freeze explicit participant-profile history in Dataset Schema v6.

Revision ID: 0033_dataset_v6_profile_history
Revises: 0032_stage5_causal_hardening
"""

from alembic import op


revision = "0033_dataset_v6_profile_history"
down_revision = "0032_stage5_causal_hardening"
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
        "'care_intervention_exposure', 'warning_delivery', "
        "'participant_profile')",
    )
    # Pre-v6 candidates lack split-causal BRS/Explicit Profile provenance and
    # must be replayed instead of being silently treated as v6 candidates.
    op.execute(
        "UPDATE parameter_learning_runs SET status = 'rejected' "
        "WHERE model_family = 'hierarchical-ctssm-residual.v2' "
        "AND status = 'candidate'"
    )
    op.execute(
        "UPDATE learned_model_profiles SET validation_status = 'rejected' "
        "WHERE model_version = 'mindflow-ctssm-runtime-v10' "
        "AND validation_status = 'candidate'"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        type_="check",
    )
    op.execute(
        "DELETE FROM dataset_snapshot_items "
        "WHERE item_type = 'participant_profile'"
    )
    op.create_check_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        "item_type IN ('participant', 'observation', 'forecast', "
        "'forecast_currentness', 'calendar', 'match_source', "
        "'psychometric', 'daily_review', 'slow_state', "
        "'care_intervention_exposure', 'warning_delivery')",
    )
