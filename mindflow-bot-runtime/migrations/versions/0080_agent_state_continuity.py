"""Add backend state continuity ledger and Agent cursor.

Revision ID: 0080_agent_state_continuity
Revises: 0079_semantic_communication_rules
"""

from alembic import op
import sqlalchemy as sa


revision = "0080_agent_state_continuity"
down_revision = "0079_semantic_communication_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "claude_sessions",
        sa.Column("last_backend_state_event_id", sa.Uuid(), nullable=True),
    )
    op.create_table(
        "participant_agent_state_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_state_event_participant_created",
        "participant_agent_state_events",
        ["participant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_state_event_participant_created",
        table_name="participant_agent_state_events",
    )
    op.drop_table("participant_agent_state_events")
    op.drop_column("claude_sessions", "last_backend_state_event_id")
