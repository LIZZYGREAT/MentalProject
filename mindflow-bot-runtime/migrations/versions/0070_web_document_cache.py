"""Add short-lived participant-bound public web document cache.

Revision ID: 0070_web_document_cache
Revises: 0069_card_action_receipt_minimization
"""

from alembic import op
import sqlalchemy as sa


revision = "0070_web_document_cache"
down_revision = "0069_card_action_receipt_minimization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_web_document_participant_url_expiry",
        "web_documents",
        ["participant_id", "url_hash", "expires_at"],
        unique=False,
    )
    op.create_table(
        "web_document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["web_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name="uq_web_document_chunk_index"
        ),
    )
    op.create_index(
        "ix_web_document_chunk_expiry",
        "web_document_chunks",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_web_document_chunk_expiry", table_name="web_document_chunks")
    op.drop_table("web_document_chunks")
    op.drop_index(
        "ix_web_document_participant_url_expiry", table_name="web_documents"
    )
    op.drop_table("web_documents")
