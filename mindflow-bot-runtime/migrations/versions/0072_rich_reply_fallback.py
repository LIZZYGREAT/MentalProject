"""Persist rich reply transport and clean plain fallback.

Revision ID: 0072_rich_reply_fallback
Revises: 0071_streaming_reply_plan
"""

from alembic import op
import sqlalchemy as sa


revision = "0072_rich_reply_fallback"
down_revision = "0071_streaming_reply_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_events",
        sa.Column("reply_presentation_mode", sa.String(32), nullable=True),
    )
    op.add_column("bot_events", sa.Column("reply_rich_text", sa.Text(), nullable=True))
    op.add_column(
        "bot_events",
        sa.Column("reply_plain_text_fallback", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bot_events", "reply_plain_text_fallback")
    op.drop_column("bot_events", "reply_rich_text")
    op.drop_column("bot_events", "reply_presentation_mode")
