"""Add a short-lived participant-bound public video cache.

Revision ID: 0081_public_video_cache
Revises: 0080_agent_state_continuity
"""

from alembic import op
import sqlalchemy as sa


revision = "0081_public_video_cache"
down_revision = "0080_agent_state_continuity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_video_caches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("video_id", sa.String(length=128), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=120), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.String(length=64), nullable=True),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("subtitle_version", sa.String(length=32), nullable=False),
        sa.Column("transcript_json", sa.Text(), nullable=False),
        sa.Column("total_chars", sa.Integer(), nullable=False),
        sa.Column("extraction_mode", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_public_video_cache_participant_video_expiry",
        "public_video_caches",
        ["participant_id", "video_id", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_public_video_cache_participant_video_expiry",
        table_name="public_video_caches",
    )
    op.drop_table("public_video_caches")
