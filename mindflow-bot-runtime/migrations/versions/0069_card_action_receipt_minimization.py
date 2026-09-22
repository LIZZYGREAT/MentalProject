"""Minimize durable CardAction receipts and add bounded retention.

Revision ID: 0069_card_action_receipt_minimization
Revises: 0068_card_action_receipts
"""

from alembic import op
import sqlalchemy as sa


revision = "0069_card_action_receipt_minimization"
down_revision = "0068_card_action_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "card_action_receipts",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE card_action_receipts "
        "SET expires_at = created_at + INTERVAL '168 hours' "
        "WHERE expires_at IS NULL"
    )
    op.alter_column(
        "card_action_receipts",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.drop_column("card_action_receipts", "result_json")
    op.create_index(
        "ix_card_action_receipt_expiry",
        "card_action_receipts",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_card_action_receipt_expiry",
        table_name="card_action_receipts",
    )
    op.add_column(
        "card_action_receipts",
        sa.Column("result_json", sa.JSON(), nullable=True),
    )
    op.drop_column("card_action_receipts", "expires_at")
