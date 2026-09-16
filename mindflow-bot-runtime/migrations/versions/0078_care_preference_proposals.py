"""Allow reviewed care preference proposals.

Revision ID: 0078_care_preference_proposals
Revises: 0077_personalization_proposals
"""

from alembic import op


revision = "0078_care_preference_proposals"
down_revision = "0077_personalization_proposals"
branch_labels = None
depends_on = None


def _replace_domain_constraint(expression: str) -> None:
    with op.batch_alter_table("personalization_proposals") as batch:
        batch.drop_constraint(
            "ck_personalization_proposal_domain", type_="check"
        )
        batch.create_check_constraint(
            "ck_personalization_proposal_domain", expression
        )


def upgrade() -> None:
    _replace_domain_constraint(
        "domain IN ('memory', 'interaction_preferences', "
        "'support_preferences', 'care_preferences')"
    )


def downgrade() -> None:
    _replace_domain_constraint(
        "domain IN ('memory', 'interaction_preferences', 'support_preferences')"
    )
