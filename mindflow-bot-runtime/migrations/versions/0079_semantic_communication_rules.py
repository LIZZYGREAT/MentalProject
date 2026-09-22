"""Add participant-reviewed semantic communication rules.

Revision ID: 0079_semantic_communication_rules
Revises: 0078_care_preference_proposals
"""

from alembic import op
import sqlalchemy as sa


revision = "0079_semantic_communication_rules"
down_revision = "0078_care_preference_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_interaction_semantic_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_proposal_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('all_responses', 'explanations', 'technical_explanations', 'code_and_engineering')",
            name="ck_semantic_rule_scope",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'deleted')",
            name="ck_semantic_rule_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_proposal_id"], ["personalization_proposals.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_semantic_rule_active",
        "participant_interaction_semantic_rules",
        ["participant_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_semantic_rule_active",
        table_name="participant_interaction_semantic_rules",
    )
    op.drop_table("participant_interaction_semantic_rules")
