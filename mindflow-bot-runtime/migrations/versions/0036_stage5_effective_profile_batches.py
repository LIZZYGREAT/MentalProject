"""Add durable Dataset batch identity for Stage-5 weekly calibration.

Revision ID: 0036_stage5_effective_profile
Revises: 0035_stage5_v7_runtime_cutover
"""

from alembic import op
import sqlalchemy as sa


revision = "0036_stage5_effective_profile"
down_revision = "0035_stage5_v7_runtime_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dataset_snapshots",
        sa.Column("purpose", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dataset_snapshots",
        sa.Column("schedule_key", sa.String(length=32), nullable=True),
    )
    op.execute(
        "UPDATE dataset_snapshots SET purpose = 'manual_research' "
        "WHERE purpose IS NULL"
    )
    op.alter_column(
        "dataset_snapshots",
        "purpose",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_dataset_snapshot_batch_identity",
        "dataset_snapshots",
        "(purpose = 'stage5_weekly_calibration' AND schedule_key IS NOT NULL) "
        "OR (purpose <> 'stage5_weekly_calibration' AND schedule_key IS NULL)",
    )
    op.create_index(
        "uq_dataset_snapshot_weekly_batch",
        "dataset_snapshots",
        ["purpose", "schedule_key"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'stage5_weekly_calibration'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_dataset_snapshot_weekly_batch",
        table_name="dataset_snapshots",
    )
    op.drop_constraint(
        "ck_dataset_snapshot_batch_identity",
        "dataset_snapshots",
        type_="check",
    )
    op.drop_column("dataset_snapshots", "schedule_key")
    op.drop_column("dataset_snapshots", "purpose")
