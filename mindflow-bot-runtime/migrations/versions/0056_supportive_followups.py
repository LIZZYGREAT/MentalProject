"""Add TTL-bounded supportive follow-up candidates.

Revision ID: 0056_supportive_followups
Revises: 0055_reminders
"""

from alembic import op
import sqlalchemy as sa

revision = "0056_supportive_followups"
down_revision = "0055_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "care_followup_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("reason_category", sa.String(length=32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reason_category IN ('check_in', 'task_transition', 'recovery')", name="ck_care_followup_reason_category"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_care_followup_due", "care_followup_candidates", ["status", "due_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_care_followup_due", table_name="care_followup_candidates")
    op.drop_table("care_followup_candidates")
