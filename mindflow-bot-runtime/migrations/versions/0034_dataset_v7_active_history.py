"""Freeze learned active history and revoke legacy Stage-5 promotions.

Revision ID: 0034_dataset_v7_active_history
Revises: 0033_dataset_v6_profile_history
"""

from alembic import op


revision = "0034_dataset_v7_active_history"
down_revision = "0033_dataset_v6_profile_history"
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
        "'participant_profile', 'learned_model_profile')",
    )
    # Preserve all rows, but remove production eligibility from Stage-5 v10
    # promotions that predate the v6 causal gate and formal replay v2.
    op.execute(
        "UPDATE parameter_learning_runs AS runs SET status = 'rejected' "
        "WHERE runs.status = 'promoted' AND EXISTS ("
        "SELECT 1 FROM learned_model_profiles AS profiles "
        "WHERE profiles.model_version = 'mindflow-ctssm-runtime-v10' "
        "AND profiles.validation_status = 'validated' "
        "AND profiles.parameters_json -> 'model_selection' ->> 'status' "
        "= 'stage5_promoted' "
        "AND profiles.parameters_json -> 'model_selection' "
        "->> 'parameter_learning_run_id' = CAST(runs.id AS TEXT))"
    )
    op.execute(
        "UPDATE learned_model_profiles SET validation_status = 'rejected' "
        "WHERE model_version = 'mindflow-ctssm-runtime-v10' "
        "AND validation_status = 'validated' "
        "AND parameters_json -> 'model_selection' ->> 'status' "
        "= 'stage5_promoted'"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        type_="check",
    )
    op.execute(
        "DELETE FROM dataset_snapshot_items "
        "WHERE item_type = 'learned_model_profile'"
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
    # Revoked production eligibility is intentionally not restored.
