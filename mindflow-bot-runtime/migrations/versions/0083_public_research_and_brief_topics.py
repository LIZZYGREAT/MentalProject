"""Add public research evidence and confirmed morning-brief topics."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0083_public_research_and_brief_topics"
down_revision = "0082_public_video_resource_key"
branch_labels = None
depends_on = None

JSON_VALUE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "participant_morning_brief_preferences",
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("include_calendar", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("include_reminders", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_research_items", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("lookback_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("language", sa.String(length=32), nullable=False, server_default="zh-CN"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("participant_id"),
    )
    op.create_table(
        "participant_morning_brief_topics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("topic_label", sa.String(length=160), nullable=False),
        sa.Column("query_hints_json", JSON_VALUE, nullable=False),
        sa.Column("source_kinds_json", JSON_VALUE, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "topic_label", name="uq_morning_brief_topic_label"),
    )
    op.create_index(
        "ix_morning_brief_topic_participant_enabled",
        "participant_morning_brief_topics",
        ["participant_id", "enabled", "priority"],
        unique=False,
    )
    op.create_table(
        "research_job_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("topic_hash", sa.String(length=64), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("browser_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exec_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("failure_reason", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_research_job_participant_started", "research_job_audits", ["participant_id", "started_at"], unique=False)
    op.create_table(
        "research_evidence",
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("topic_label", sa.String(length=160), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("publisher", sa.String(length=160), nullable=True),
        sa.Column("published_at", sa.String(length=64), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("extraction_mode", sa.String(length=32), nullable=False),
        sa.Column("freshness_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("verified_public_source", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("evidence_id"),
    )
    op.create_index("ix_research_evidence_participant_retrieved", "research_evidence", ["participant_id", "retrieved_at"], unique=False)
    op.create_index("ix_research_evidence_content_hash", "research_evidence", ["participant_id", "content_hash"], unique=False)
    with op.batch_alter_table("personalization_proposals") as batch:
        batch.drop_constraint("ck_personalization_proposal_domain", type_="check")
        batch.create_check_constraint(
            "ck_personalization_proposal_domain",
            "domain IN ('memory', 'interaction_preferences', 'support_preferences', 'care_preferences', 'morning_brief_topics')",
        )


def downgrade() -> None:
    with op.batch_alter_table("personalization_proposals") as batch:
        batch.drop_constraint("ck_personalization_proposal_domain", type_="check")
        batch.create_check_constraint(
            "ck_personalization_proposal_domain",
            "domain IN ('memory', 'interaction_preferences', 'support_preferences', 'care_preferences')",
        )
    op.drop_index("ix_research_evidence_content_hash", table_name="research_evidence")
    op.drop_index("ix_research_evidence_participant_retrieved", table_name="research_evidence")
    op.drop_table("research_evidence")
    op.drop_index("ix_research_job_participant_started", table_name="research_job_audits")
    op.drop_table("research_job_audits")
    op.drop_index("ix_morning_brief_topic_participant_enabled", table_name="participant_morning_brief_topics")
    op.drop_table("participant_morning_brief_topics")
    op.drop_table("participant_morning_brief_preferences")

