"""Add versioned learned model profiles.

Revision ID: 0009_learned_model_profiles
Revises: 0008_forecast_observation_revision
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_learned_model_profiles"
down_revision = "0008_forecast_observation_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learned_model_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parameters_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("day_count", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "version", name="uq_learned_profile_version"),
    )
    op.create_index(
        "ix_learned_profile_participant_version", "learned_model_profiles",
        ["participant_id", "version"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_learned_profile_participant_version", table_name="learned_model_profiles")
    op.drop_table("learned_model_profiles")
