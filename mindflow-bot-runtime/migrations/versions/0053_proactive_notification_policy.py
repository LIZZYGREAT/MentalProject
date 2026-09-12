"""Add global proactive policy preferences and delivery ledger.

Revision ID: 0053_proactive_notification_policy
Revises: 0052_bot_event_content_privacy
"""

from alembic import op
import sqlalchemy as sa


revision = "0053_proactive_notification_policy"
down_revision = "0052_bot_event_content_privacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participant_care_preferences", sa.Column("morning_brief_local_time", sa.Time(), nullable=False, server_default="08:00:00"))
    op.add_column("participant_care_preferences", sa.Column("morning_brief_paused_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("participant_care_preferences", sa.Column("weekly_summary_local_time", sa.Time(), nullable=False, server_default="09:00:00"))
    op.add_column("participant_care_preferences", sa.Column("weekly_summary_weekday", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("participant_care_preferences", sa.Column("max_system_proactive_per_day", sa.Integer(), nullable=True))
    op.add_column("participant_care_preferences", sa.Column("global_proactive_muted_until", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "proactive_notification_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("message_class", sa.String(length=32), nullable=False),
        sa.Column("message_kind", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("suppression_reason", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("message_class IN ('system_proactive', 'user_requested')", name="ck_proactive_notification_class"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "message_kind", "dedupe_key", name="uq_proactive_notification_dedupe"),
    )
    op.create_index("ix_proactive_notification_budget", "proactive_notification_deliveries", ["participant_id", "message_class", "scheduled_at", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_proactive_notification_budget", table_name="proactive_notification_deliveries")
    op.drop_table("proactive_notification_deliveries")
    for column in ("global_proactive_muted_until", "max_system_proactive_per_day", "weekly_summary_weekday", "weekly_summary_local_time", "morning_brief_paused_until", "morning_brief_local_time"):
        op.drop_column("participant_care_preferences", column)
