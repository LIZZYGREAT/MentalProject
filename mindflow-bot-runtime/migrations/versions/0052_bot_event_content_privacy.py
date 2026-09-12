"""Protect Safety-handled content in researcher/admin message views.

Revision ID: 0052_bot_event_content_privacy
Revises: 0051_participant_consents
"""

from alembic import op
import sqlalchemy as sa


revision = "0052_bot_event_content_privacy"
down_revision = "0051_participant_consents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_events",
        sa.Column(
            "content_privacy_class",
            sa.String(length=16),
            nullable=False,
            server_default="normal",
        ),
    )


def downgrade() -> None:
    op.drop_column("bot_events", "content_privacy_class")
