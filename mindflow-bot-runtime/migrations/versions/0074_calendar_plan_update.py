"""Allow one-item and update Calendar mutation plans.

Revision ID: 0074_calendar_plan_update
Revises: 0073_web_document_extraction_mode
"""

from alembic import op
import sqlalchemy as sa


revision = "0074_calendar_plan_update"
down_revision = "0073_web_document_extraction_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_calendar_mutation_plan_operation",
        "calendar_mutation_plans",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calendar_mutation_plan_operation",
        "calendar_mutation_plans",
        "operation IN ('create','update','delete')",
    )
    op.drop_constraint(
        "ck_calendar_mutation_plan_item_operation",
        "calendar_mutation_plan_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calendar_mutation_plan_item_operation",
        "calendar_mutation_plan_items",
        "operation IN ('create','update','delete')",
    )
    op.drop_constraint(
        "ck_calendar_mutation_plan_item_status",
        "calendar_mutation_plan_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calendar_mutation_plan_item_status",
        "calendar_mutation_plan_items",
        "status IN ('pending','creating','updating','deleting','succeeded',"
        "'failed','outcome_unknown')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    unsupported = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM calendar_mutation_plans p "
                "LEFT JOIN calendar_mutation_plan_items i ON i.plan_id = p.id "
                "WHERE p.operation = 'update' OR i.operation = 'update' "
                "OR i.status = 'updating'"
            )
        ).scalar_one()
    )
    if unsupported:
        raise RuntimeError(
            "0074 downgrade refuses while update Calendar plans exist"
        )
    op.drop_constraint(
        "ck_calendar_mutation_plan_item_status",
        "calendar_mutation_plan_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calendar_mutation_plan_item_status",
        "calendar_mutation_plan_items",
        "status IN ('pending','creating','deleting','succeeded','failed',"
        "'outcome_unknown')",
    )
    op.drop_constraint(
        "ck_calendar_mutation_plan_item_operation",
        "calendar_mutation_plan_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calendar_mutation_plan_item_operation",
        "calendar_mutation_plan_items",
        "operation IN ('create','delete')",
    )
    op.drop_constraint(
        "ck_calendar_mutation_plan_operation",
        "calendar_mutation_plans",
        type_="check",
    )
    op.create_check_constraint(
        "ck_calendar_mutation_plan_operation",
        "calendar_mutation_plans",
        "operation IN ('create','delete')",
    )
