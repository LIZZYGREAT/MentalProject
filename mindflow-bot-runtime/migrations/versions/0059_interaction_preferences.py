"""Add structured interaction preferences and bounded rules.

Revision ID: 0059_interaction_preferences
Revises: 0058_participant_memory
"""

from alembic import op
import sqlalchemy as sa

revision = "0059_interaction_preferences"
down_revision = "0058_participant_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_interaction_styles",
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("verbosity", sa.String(length=16), nullable=False),
        sa.Column("tone", sa.String(length=16), nullable=False),
        sa.Column("suggestion_style", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("participant_id"),
    )
    op.create_table(
        "participant_interaction_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("raw_text", sa.String(length=200), nullable=False),
        sa.Column("normalized_category", sa.String(length=32), nullable=False),
        sa.Column("normalized_value", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_interaction_rule_active", "participant_interaction_rules", ["participant_id", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_interaction_rule_active", table_name="participant_interaction_rules")
    op.drop_table("participant_interaction_rules")
    op.drop_table("participant_interaction_styles")
