"""Add participant care preferences and intervention feedback loop.

Revision ID: 0016_care_intervention_feedback
Revises: 0015_daily_review_causal_source
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016_care_intervention_feedback"
down_revision = "0015_daily_review_causal_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "participant_care_preferences",
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("care_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("warning_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("daily_review_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("morning_brief_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("weekly_summary_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("quiet_hours_start", sa.Time(), nullable=True),
        sa.Column("quiet_hours_end", sa.Time(), nullable=True),
        sa.Column("max_proactive_care_per_day", sa.Integer(), nullable=True),
        sa.Column("allow_schedule_suggestions", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("allow_follow_up", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "preferred_support_types",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("participant_id"),
    )
    op.create_table(
        "care_intervention_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("source_warning_id", sa.Uuid(), nullable=False),
        sa.Column("source_forecast_id", sa.Uuid(), nullable=False),
        sa.Column("forecast_version", sa.String(length=64), nullable=False),
        sa.Column("intervention_type", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=128), nullable=False),
        sa.Column("template_version", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("delivery_status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("user_action", sa.String(length=32), nullable=True),
        sa.Column("action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column(
            "context_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "actions_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_warning_id"], ["warning_schedules.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_forecast_id"], ["forecast_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_warning_id", name="uq_care_intervention_warning"
        ),
    )
    op.create_index(
        "ix_care_intervention_participant_scheduled",
        "care_intervention_events",
        ["participant_id", "scheduled_at"],
    )
    op.create_index(
        "ix_care_intervention_status_scheduled",
        "care_intervention_events",
        ["status", "scheduled_at"],
    )
    # Existing Warning rows are audit history too. Seed one normalized Care
    # event per Warning so the timeline is complete immediately after upgrade;
    # later transitions continue through the transactional repository mirror.
    op.execute(
        """
        INSERT INTO care_intervention_events (
            id,
            participant_id,
            source_warning_id,
            source_forecast_id,
            forecast_version,
            intervention_type,
            template_id,
            template_version,
            reason_code,
            scheduled_at,
            sent_at,
            status,
            delivery_status,
            user_action,
            action_at,
            snoozed_until,
            message_text,
            context_json,
            actions_json,
            created_at,
            updated_at
        )
        SELECT
            warning.id,
            warning.participant_id,
            warning.id,
            warning.forecast_id,
            warning.forecast_version,
            COALESCE(
                warning.payload_json -> 'care_plan' ->> 'intervention_type',
                'generic_fallback'
            ),
            COALESCE(
                warning.payload_json -> 'care_provenance' ->> 'template_id',
                warning.payload_json -> 'care_plan' ->> 'template_id',
                'legacy-fallback'
            ),
            COALESCE(
                warning.payload_json -> 'care_provenance' ->> 'template_version',
                '1.0.0'
            ),
            COALESCE(
                warning.payload_json -> 'care_plan' ->> 'reason_code',
                'forecast_warning'
            ),
            warning.target_time,
            warning.sent_at,
            CASE
                WHEN warning.status IN ('sent', 'escalated') THEN 'sent'
                ELSE warning.status
            END,
            warning.status,
            NULL,
            NULL,
            NULL,
            COALESCE(
                warning.payload_json ->> 'message',
                warning.payload_json ->> 'fallback_message',
                ''
            ),
            jsonb_build_object(
                'care_context', COALESCE(
                    warning.payload_json -> 'care_context', '{}'::jsonb
                ),
                'care_plan', COALESCE(
                    warning.payload_json -> 'care_plan', '{}'::jsonb
                ),
                'care_provenance', COALESCE(
                    warning.payload_json -> 'care_provenance', '{}'::jsonb
                )
            ),
            COALESCE(
                warning.payload_json -> 'care_plan' -> 'actions',
                '["ack", "snooze_30", "mute_today", "helpful", "not_relevant"]'::jsonb
            ),
            warning.updated_at,
            warning.updated_at
        FROM warning_schedules AS warning
        ON CONFLICT (source_warning_id) DO NOTHING
        """
    )
    op.create_table(
        "care_intervention_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("intervention_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("helpfulness", sa.String(length=32), nullable=True),
        sa.Column("relevance", sa.String(length=32), nullable=True),
        sa.Column("timing_feedback", sa.String(length=32), nullable=True),
        sa.Column("action_selected", sa.String(length=32), nullable=False),
        sa.Column("optional_comment", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("callback_event_id", sa.String(length=160), nullable=False),
        sa.ForeignKeyConstraint(
            ["intervention_id"], ["care_intervention_events.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("callback_event_id"),
    )
    op.create_index(
        "ix_care_feedback_participant_submitted",
        "care_intervention_feedback",
        ["participant_id", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_care_feedback_participant_submitted",
        table_name="care_intervention_feedback",
    )
    op.drop_table("care_intervention_feedback")
    op.drop_index(
        "ix_care_intervention_status_scheduled",
        table_name="care_intervention_events",
    )
    op.drop_index(
        "ix_care_intervention_participant_scheduled",
        table_name="care_intervention_events",
    )
    op.drop_table("care_intervention_events")
    op.drop_table("participant_care_preferences")
