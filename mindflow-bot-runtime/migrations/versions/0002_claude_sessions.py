"""Persist participant-scoped Claude Agent SDK sessions.

Revision ID: 0002_claude_sessions
Revises: 0001_production_runtime
"""

from alembic import op


revision = "0002_claude_sessions"
down_revision = "0001_production_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE claude_sessions (
            participant_id UUID PRIMARY KEY
                REFERENCES participants(id) ON DELETE CASCADE,
            session_id VARCHAR(128) UNIQUE NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            last_message_id VARCHAR(128) NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS claude_sessions CASCADE")
