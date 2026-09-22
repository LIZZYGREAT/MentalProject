"""Add durable CardAction callback receipts.

Revision ID: 0068_card_action_receipts
Revises: 0067_web_search_provider_audit
"""

from alembic import op
import sqlalchemy as sa


revision = "0068_card_action_receipts"
down_revision = "0067_web_search_provider_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "card_action_receipts",
        sa.Column("event_id", sa.String(length=256), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("action_name", sa.String(length=64), nullable=False),
        sa.Column("action_version", sa.String(length=16), nullable=False),
        sa.Column("action_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("message_id_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_kind", sa.String(length=32), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing', 'succeeded', 'rejected', 'failed')",
            name="ck_card_action_receipt_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_card_action_receipt_participant_created",
        "card_action_receipts",
        ["participant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_card_action_receipt_participant_created",
        table_name="card_action_receipts",
    )
    op.drop_table("card_action_receipts")
