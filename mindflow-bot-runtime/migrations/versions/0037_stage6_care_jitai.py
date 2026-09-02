"""Add Stage-6 JITAI decision, proximal outcome, and MRT-ready contracts.

Revision ID: 0037_stage6_care_jitai
Revises: 0036_stage5_effective_profile
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0037_stage6_care_jitai"
down_revision = "0036_stage5_effective_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_default_list = sa.text("'[]'::jsonb")
    op.add_column(
        "warning_schedules",
        sa.Column("authorization_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE warning_schedules "
        "SET authorization_deadline = risk_time "
        "WHERE authorization_deadline IS NULL"
    )
    op.alter_column(
        "warning_schedules", "authorization_deadline", nullable=False
    )
    op.add_column(
        "participant_care_preferences",
        sa.Column("inferred_support_types", postgresql.JSONB(), server_default=json_default_list, nullable=False),
    )
    op.add_column(
        "participant_care_preferences",
        sa.Column("disabled_intervention_types", postgresql.JSONB(), server_default=json_default_list, nullable=False),
    )
    op.add_column(
        "participant_care_preferences",
        sa.Column("interruption_tolerance", sa.Float(), server_default="0.5", nullable=False),
    )
    op.create_check_constraint(
        "ck_care_interruption_tolerance",
        "participant_care_preferences",
        "interruption_tolerance BETWEEN 0 AND 1",
    )
    op.add_column(
        "participant_care_preferences",
        sa.Column("preferred_reminder_windows", postgresql.JSONB(), server_default=json_default_list, nullable=False),
    )
    op.add_column("care_intervention_events", sa.Column("vulnerability_score", sa.Float(), nullable=True))
    op.add_column("care_intervention_events", sa.Column("receptivity_score", sa.Float(), nullable=True))
    op.add_column("care_intervention_events", sa.Column("decision_score", sa.Float(), nullable=True))
    op.add_column(
        "care_intervention_events",
        sa.Column("decision_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_check_constraint(
        "ck_care_jitai_scores",
        "care_intervention_events",
        "(vulnerability_score IS NULL OR vulnerability_score BETWEEN 0 AND 1) AND "
        "(receptivity_score IS NULL OR receptivity_score BETWEEN 0 AND 1) AND "
        "(decision_score IS NULL OR decision_score BETWEEN 0 AND 1)",
    )
    op.create_table(
        "care_intervention_outcomes",
        sa.Column("intervention_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_state", postgresql.JSONB(), nullable=False),
        sa.Column("followup_30m", postgresql.JSONB(), nullable=True),
        sa.Column("followup_60m", postgresql.JSONB(), nullable=True),
        sa.Column("helpful_rating", sa.Float(), nullable=True),
        sa.Column("user_action", sa.String(length=32), nullable=True),
        sa.Column("context_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("helpful_rating IS NULL OR helpful_rating BETWEEN 0 AND 1", name="ck_care_outcome_helpful"),
        sa.ForeignKeyConstraint(["intervention_id"], ["care_intervention_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("intervention_id"),
    )
    op.create_index(
        "ix_care_outcome_participant_created",
        "care_intervention_outcomes",
        ["participant_id", "created_at"],
    )
    op.create_table(
        "intervention_randomization_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("decision_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("eligibility", sa.Boolean(), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("candidate_actions", postgresql.JSONB(), nullable=False),
        sa.Column("assigned_action", sa.String(length=64), nullable=False),
        sa.Column("randomization_probability", sa.Float(), nullable=False),
        sa.Column("proximal_outcome", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("randomization_probability BETWEEN 0 AND 1", name="ck_mrt_probability"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mrt_participant_decision",
        "intervention_randomization_events",
        ["participant_id", "decision_time"],
    )


def downgrade() -> None:
    op.drop_index("ix_mrt_participant_decision", table_name="intervention_randomization_events")
    op.drop_table("intervention_randomization_events")
    op.drop_index("ix_care_outcome_participant_created", table_name="care_intervention_outcomes")
    op.drop_table("care_intervention_outcomes")
    op.drop_constraint("ck_care_jitai_scores", "care_intervention_events", type_="check")
    op.drop_column("care_intervention_events", "decision_json")
    op.drop_column("care_intervention_events", "decision_score")
    op.drop_column("care_intervention_events", "receptivity_score")
    op.drop_column("care_intervention_events", "vulnerability_score")
    op.drop_column("participant_care_preferences", "preferred_reminder_windows")
    op.drop_constraint(
        "ck_care_interruption_tolerance",
        "participant_care_preferences",
        type_="check",
    )
    op.drop_column("participant_care_preferences", "interruption_tolerance")
    op.drop_column("participant_care_preferences", "disabled_intervention_types")
    op.drop_column("participant_care_preferences", "inferred_support_types")
    op.drop_column("warning_schedules", "authorization_deadline")
