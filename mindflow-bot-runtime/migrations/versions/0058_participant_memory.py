"""Add explicit participant memory store.

Revision ID: 0058_participant_memory
Revises: 0057_controlled_web_search
"""

from alembic import op
import sqlalchemy as sa

revision = "0058_participant_memory"
down_revision = "0057_controlled_web_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_memory_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("memory_type", sa.String(length=32), nullable=False),
        sa.Column("content", sa.String(length=500), nullable=False),
        sa.Column("normalized_content", sa.String(length=500), nullable=False),
        sa.Column("conflict_key", sa.String(length=80), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("consent_basis", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("memory_type IN ('preference', 'stable_fact', 'goal', 'routine', 'support_preference', 'context')", name="ck_memory_type"),
        sa.CheckConstraint("source IN ('user_explicit', 'assistant_summary', 'system_candidate')", name="ck_memory_source"),
        sa.CheckConstraint("consent_basis IN ('user_requested_memory', 'explicit_setting', 'candidate_only')", name="ck_memory_consent_basis"),
        sa.CheckConstraint("status IN ('active', 'superseded', 'deleted', 'candidate')", name="ck_memory_status"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["superseded_by"], ["participant_memory_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_participant_status", "participant_memory_items", ["participant_id", "status", "updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memory_participant_status", table_name="participant_memory_items")
    op.drop_table("participant_memory_items")
