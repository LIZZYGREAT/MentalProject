"""Include state observations in durable forecast cache identity.

Revision ID: 0008_forecast_observation_revision
Revises: 0007_calendar_oauth_app_identity
"""

from alembic import op


revision = "0008_forecast_observation_revision"
down_revision = "0007_calendar_oauth_app_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE forecast_snapshots "
        "ADD COLUMN observation_revision VARCHAR(64) NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE forecast_snapshots "
        "ALTER COLUMN observation_revision DROP DEFAULT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE forecast_snapshots "
        "DROP COLUMN IF EXISTS observation_revision"
    )
