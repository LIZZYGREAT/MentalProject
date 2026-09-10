"""Persist participant-bound course schedule image sessions.

Revision ID: 0046_course_schedule_image_sessions
Revises: 0045_course_schedule_compensation_refresh_outbox
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0046_course_schedule_image_sessions"
down_revision = "0045_course_schedule_compensation_refresh_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "course_schedule_image_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=True),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("image_message_id", sa.String(length=128), nullable=False),
        sa.Column("image_key", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("parse_report_json", postgresql.JSONB(), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column("vision_model", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('parsing','needs_information','needs_retry','ready',"
            "'imported','archived')",
            name="ck_course_schedule_image_session_status",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["import_id"], ["course_schedule_imports.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "participant_id",
            "image_message_id",
            name="uq_course_schedule_image_session_message",
        ),
    )
    op.create_index(
        "ix_course_schedule_image_session_participant_chat",
        "course_schedule_image_sessions",
        ["participant_id", "chat_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_course_schedule_image_session_participant_chat",
        table_name="course_schedule_image_sessions",
    )
    op.drop_table("course_schedule_image_sessions")
