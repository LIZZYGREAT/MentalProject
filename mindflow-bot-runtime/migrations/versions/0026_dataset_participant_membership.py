"""Allow dataset snapshots to freeze participant membership.

Revision ID: 0026_dataset_participant_membership
Revises: 0025_dataset_snapshot_items
"""

from alembic import op


revision = "0026_dataset_participant_membership"
down_revision = "0025_dataset_snapshot_items"
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
        "'forecast_currentness', 'calendar', 'match_source')",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM dataset_snapshot_items WHERE item_type = 'participant'"
    )
    op.drop_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_dataset_snapshot_item_type",
        "dataset_snapshot_items",
        "item_type IN ('observation', 'forecast', "
        "'forecast_currentness', 'calendar', 'match_source')",
    )
