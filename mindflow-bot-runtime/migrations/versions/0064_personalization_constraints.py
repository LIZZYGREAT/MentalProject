"""Add database constraints for structured personalization values.

Revision ID: 0064_personalization_constraints
Revises: 0063_web_search_query_privacy
"""

from alembic import op


revision = "0064_personalization_constraints"
down_revision = "0063_web_search_query_privacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_interaction_style_verbosity", "participant_interaction_styles",
        "verbosity IN ('concise', 'balanced', 'detailed')",
    )
    op.create_check_constraint(
        "ck_interaction_style_tone", "participant_interaction_styles",
        "tone IN ('neutral', 'warm', 'direct')",
    )
    op.create_check_constraint(
        "ck_interaction_style_suggestion", "participant_interaction_styles",
        "suggestion_style IN ('ask_first', 'light_suggestions', 'proactive_suggestions')",
    )
    op.create_check_constraint(
        "ck_interaction_rule_status", "participant_interaction_rules",
        "status IN ('active', 'superseded', 'deleted')",
    )
    op.create_check_constraint(
        "ck_support_preference_max_suggestions", "participant_support_preferences",
        "max_suggestions BETWEEN 1 AND 3",
    )
    op.create_check_constraint(
        "ck_support_preference_style", "participant_support_preferences",
        "preferred_support_style IN ('gentle', 'practical', 'listening')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_support_preference_style", "participant_support_preferences", type_="check"
    )
    op.drop_constraint(
        "ck_support_preference_max_suggestions", "participant_support_preferences",
        type_="check",
    )
    op.drop_constraint(
        "ck_interaction_rule_status", "participant_interaction_rules", type_="check"
    )
    op.drop_constraint(
        "ck_interaction_style_suggestion", "participant_interaction_styles", type_="check"
    )
    op.drop_constraint(
        "ck_interaction_style_tone", "participant_interaction_styles", type_="check"
    )
    op.drop_constraint(
        "ck_interaction_style_verbosity", "participant_interaction_styles", type_="check"
    )
