"""Add a generation token for warning delivery claims.

Revision ID: 0006_warning_claim_token
Revises: 0005_fix_legacy_warning_times
"""

from alembic import op


revision = "0006_warning_claim_token"
down_revision = "0005_fix_legacy_warning_times"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE warning_schedules ADD COLUMN claim_token UUID NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE warning_schedules DROP COLUMN IF EXISTS claim_token")
