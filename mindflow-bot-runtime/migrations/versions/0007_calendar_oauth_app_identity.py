"""Record the Feishu app that owns Calendar OAuth state.

Revision ID: 0007_calendar_oauth_app_identity
Revises: 0006_warning_claim_token
"""

from alembic import op


revision = "0007_calendar_oauth_app_identity"
down_revision = "0006_warning_claim_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows intentionally remain NULL and require explicit reconnect.
    op.execute(
        "ALTER TABLE feishu_oauth_tokens "
        "ADD COLUMN oauth_app_id VARCHAR(128) NULL"
    )
    op.execute(
        "ALTER TABLE feishu_device_flows "
        "ADD COLUMN oauth_app_id VARCHAR(128) NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE feishu_device_flows DROP COLUMN IF EXISTS oauth_app_id"
    )
    op.execute(
        "ALTER TABLE feishu_oauth_tokens DROP COLUMN IF EXISTS oauth_app_id"
    )
