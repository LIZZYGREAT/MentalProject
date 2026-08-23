"""Add durable segmented response delivery state.

Revision ID: 0010_response_delivery
Revises: 0009_learned_model_profiles
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_response_delivery"
down_revision = "0009_learned_model_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_events",
        sa.Column(
            "reply_segments_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "bot_events",
        sa.Column(
            "reply_next_segment",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "bot_events",
        sa.Column(
            "reply_message_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "bot_events",
        sa.Column("reply_plan_version", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bot_events", "reply_plan_version")
    op.drop_column("bot_events", "reply_message_ids_json")
    op.drop_column("bot_events", "reply_next_segment")
    op.drop_column("bot_events", "reply_segments_json")

