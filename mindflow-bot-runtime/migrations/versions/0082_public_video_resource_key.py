"""Add variant-aware identity to the public video cache."""

from alembic import op
import sqlalchemy as sa


revision = "0082_public_video_resource_key"
down_revision = "0081_public_video_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "public_video_caches",
        sa.Column("resource_key", sa.String(length=192), nullable=True),
    )
    op.execute(
        "UPDATE public_video_caches SET resource_key = provider || ':' || video_id"
    )
    op.alter_column("public_video_caches", "resource_key", nullable=False)
    op.create_index(
        "ix_public_video_cache_participant_resource_expiry",
        "public_video_caches",
        ["participant_id", "resource_key", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_public_video_cache_participant_resource_expiry",
        table_name="public_video_caches",
    )
    op.drop_column("public_video_caches", "resource_key")
