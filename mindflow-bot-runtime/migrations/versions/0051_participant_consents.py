"""Add participant-owned external LLM consent records.

Revision ID: 0051_participant_consents
Revises: 0050_calendar_plan_retry_backoff
"""

from alembic import op
import sqlalchemy as sa


revision = "0051_participant_consents"
down_revision = "0050_calendar_plan_retry_backoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_consents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("consent_type", sa.String(length=64), nullable=False),
        sa.Column("consent_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','revoked')",
            name="ck_participant_consent_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["participants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "participant_id",
            "consent_type",
            "consent_version",
            "consented_at",
            name="uq_participant_consent_version_time",
        ),
    )
    op.create_index(
        "ix_participant_consent_current",
        "participant_consents",
        ["participant_id", "consent_type", "consented_at"],
    )
    # Legacy participants.external_llm_consent_at is intentionally not
    # migrated: researcher/CLI-set provenance can never be claimed as user
    # consent. Existing users grant consent themselves on first use.


def downgrade() -> None:
    op.drop_index(
        "ix_participant_consent_current",
        table_name="participant_consents",
    )
    op.drop_table("participant_consents")
