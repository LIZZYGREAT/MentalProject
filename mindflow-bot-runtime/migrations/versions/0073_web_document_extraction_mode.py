"""Persist public web document extraction mode.

Revision ID: 0073_web_document_extraction_mode
Revises: 0072_rich_reply_fallback
"""

from alembic import op
import sqlalchemy as sa


revision = "0073_web_document_extraction_mode"
down_revision = "0072_rich_reply_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_documents",
        sa.Column(
            "extraction_mode",
            sa.String(32),
            nullable=False,
            server_default="article",
        ),
    )


def downgrade() -> None:
    op.drop_column("web_documents", "extraction_mode")
