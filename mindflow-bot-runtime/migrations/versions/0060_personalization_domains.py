"""Separate explicit memory from support preferences.

Revision ID: 0060_personalization_domains
Revises: 0059_interaction_preferences
"""

from alembic import op
import sqlalchemy as sa

revision = "0060_personalization_domains"
down_revision = "0059_interaction_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_memory_type", "participant_memory_items", type_="check")
    op.drop_constraint("ck_memory_source", "participant_memory_items", type_="check")
    op.drop_constraint("ck_memory_consent_basis", "participant_memory_items", type_="check")
    op.create_check_constraint(
        "ck_memory_type", "participant_memory_items",
        "memory_type IN ('stable_fact', 'goal', 'routine', 'context', 'preferred_name')",
    )
    op.create_check_constraint(
        "ck_memory_source", "participant_memory_items",
        "source IN ('user_explicit', 'system_candidate')",
    )
    op.create_check_constraint(
        "ck_memory_consent_basis", "participant_memory_items",
        "consent_basis IN ('user_requested_memory', 'candidate_only')",
    )
    op.create_table(
        "participant_support_preferences",
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("acknowledge_before_advice", sa.Boolean(), nullable=False),
        sa.Column("ask_before_suggestion", sa.Boolean(), nullable=False),
        sa.Column("max_suggestions", sa.Integer(), nullable=False),
        sa.Column("allow_supportive_follow_up", sa.Boolean(), nullable=False),
        sa.Column("preferred_support_style", sa.String(length=24), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("participant_id"),
    )


def downgrade() -> None:
    op.drop_table("participant_support_preferences")
    op.drop_constraint("ck_memory_type", "participant_memory_items", type_="check")
    op.drop_constraint("ck_memory_source", "participant_memory_items", type_="check")
    op.drop_constraint("ck_memory_consent_basis", "participant_memory_items", type_="check")
    op.create_check_constraint("ck_memory_type", "participant_memory_items", "memory_type IN ('preference', 'stable_fact', 'goal', 'routine', 'support_preference', 'context')")
    op.create_check_constraint("ck_memory_source", "participant_memory_items", "source IN ('user_explicit', 'assistant_summary', 'system_candidate')")
    op.create_check_constraint("ck_memory_consent_basis", "participant_memory_items", "consent_basis IN ('user_requested_memory', 'explicit_setting', 'candidate_only')")
