"""Persist CardKit streaming reply recovery state.

Revision ID: 0071_streaming_reply_plan
Revises: 0070_web_document_cache
"""

from alembic import op
import sqlalchemy as sa


revision = "0071_streaming_reply_plan"
down_revision = "0070_web_document_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bot_events", sa.Column("streaming_card_id", sa.String(128), nullable=True))
    op.add_column("bot_events", sa.Column("streaming_message_id", sa.String(128), nullable=True))
    op.add_column("bot_events", sa.Column("streaming_element_id", sa.String(128), nullable=True))
    op.add_column("bot_events", sa.Column("streaming_state", sa.String(32), nullable=True))
    op.add_column(
        "bot_events",
        sa.Column("streaming_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("bot_events", sa.Column("streaming_final_text_hash", sa.String(64), nullable=True))
    op.add_column(
        "bot_events",
        sa.Column("streaming_finalized_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bot_events", "streaming_finalized_at")
    op.drop_column("bot_events", "streaming_final_text_hash")
    op.drop_column("bot_events", "streaming_sequence")
    op.drop_column("bot_events", "streaming_state")
    op.drop_column("bot_events", "streaming_element_id")
    op.drop_column("bot_events", "streaming_message_id")
    op.drop_column("bot_events", "streaming_card_id")
