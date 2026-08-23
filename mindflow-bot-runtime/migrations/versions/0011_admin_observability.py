"""Add admin observability storage and query indexes.

Revision ID: 0011_admin_observability
Revises: 0010_response_delivery
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_admin_observability"
down_revision = "0010_response_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bot_events",
        sa.Column(
            "telemetry_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_bot_event_participant_received",
        "bot_events",
        ["participant_id", "received_at"],
    )
    op.create_index(
        "ix_bot_event_status_received", "bot_events", ["status", "received_at"]
    )
    op.create_index(
        "ix_agent_run_participant_started",
        "agent_runs",
        ["participant_id", "started_at"],
    )
    op.create_table(
        "runtime_incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("subsystem", sa.String(length=64), nullable=False),
        sa.Column("event_name", sa.String(length=128), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=True),
        sa.Column("bot_event_id", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "details_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["bot_event_id"], ["bot_events.event_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runtime_incident_created", "runtime_incidents", ["created_at"]
    )
    op.create_index(
        "ix_runtime_incident_severity_created",
        "runtime_incidents",
        ["severity", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_incident_severity_created", table_name="runtime_incidents"
    )
    op.drop_index("ix_runtime_incident_created", table_name="runtime_incidents")
    op.drop_table("runtime_incidents")
    op.drop_index("ix_agent_run_participant_started", table_name="agent_runs")
    op.drop_index("ix_bot_event_status_received", table_name="bot_events")
    op.drop_index("ix_bot_event_participant_received", table_name="bot_events")
    op.drop_column("bot_events", "telemetry_json")
