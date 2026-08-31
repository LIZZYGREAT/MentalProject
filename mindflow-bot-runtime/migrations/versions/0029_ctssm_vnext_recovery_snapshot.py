"""Add Stage-4 recovery and resilience evidence to dataset snapshots.

Revision ID: 0029_ctssm_vnext_recovery_snapshot
Revises: 0028_workload_causal_provenance
"""

from alembic import op


revision = "0029_ctssm_vnext_recovery_snapshot"
down_revision = "0028_workload_causal_provenance"
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
        "'psychometric', 'daily_review', 'slow_state')",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM dataset_snapshot_items "
        "WHERE item_type IN ('psychometric', 'daily_review', 'slow_state')"
    )
    op.drop_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        "item_type IN ('participant', 'observation', 'forecast', "
        "'forecast_currentness', 'calendar', 'match_source')",
    )

