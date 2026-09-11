"""Add durable course schedule import drafts and image ingress fields.

Revision ID: 0038_course_schedule_import
Revises: 0037_stage6_care_jitai
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0038_course_schedule_import"
down_revision = "0037_stage6_care_jitai"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_events",
        sa.Column("message_type", sa.String(length=16), server_default="text", nullable=False),
    )
    op.add_column("bot_events", sa.Column("image_key", sa.String(length=512), nullable=True))
    op.create_table(
        "course_schedule_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=False),
        sa.Column("source_image_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("semester_start_date", sa.Date(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("vision_model", sa.String(length=128), nullable=False),
        sa.Column("structured_result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_context','pending_confirmation','running','succeeded','partial_failed','cancelled','expired')",
            name="ck_course_schedule_import_status",
        ),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "source_message_id", name="uq_course_schedule_import_source"),
    )
    op.create_index(
        "ix_course_schedule_import_participant_status",
        "course_schedule_imports",
        ["participant_id", "status", "created_at"],
    )
    op.create_table(
        "course_schedule_import_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("course_name", sa.String(length=200), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("week_rule_json", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("calendar_event_id", sa.String(length=256), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.CheckConstraint("weekday IS NULL OR weekday BETWEEN 1 AND 7", name="ck_course_schedule_item_weekday"),
        sa.CheckConstraint("status IN ('pending','running','succeeded','failed')", name="ck_course_schedule_item_status"),
        sa.ForeignKeyConstraint(["import_id"], ["course_schedule_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "item_index", name="uq_course_schedule_item_index"),
        sa.UniqueConstraint("import_id", "normalized_key", name="uq_course_schedule_item_key"),
    )
    op.create_index(
        "ix_course_schedule_item_import_status",
        "course_schedule_import_items",
        ["import_id", "status", "item_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_course_schedule_item_import_status", table_name="course_schedule_import_items")
    op.drop_table("course_schedule_import_items")
    op.drop_index("ix_course_schedule_import_participant_status", table_name="course_schedule_imports")
    op.drop_table("course_schedule_imports")
    op.drop_column("bot_events", "image_key")
    op.drop_column("bot_events", "message_type")
