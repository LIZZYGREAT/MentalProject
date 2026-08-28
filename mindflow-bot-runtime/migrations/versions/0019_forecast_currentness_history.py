"""Persist append-only forecast currentness history.

Revision ID: 0019_forecast_currentness_history
Revises: 0018_calendar_mutation_reconciliation
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_forecast_currentness_history"
down_revision = "0018_calendar_mutation_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_currentness_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("forecast_id", sa.Uuid(), nullable=False),
        sa.Column("forecast_version", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["forecast_id"], ["forecast_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_forecast_currentness_at",
        "forecast_currentness_events",
        ["participant_id", "local_date", "occurred_at", "id"],
    )
    op.create_index(
        "ix_forecast_currentness_forecast",
        "forecast_currentness_events",
        ["forecast_id", "id"],
    )
    # Seed only the state known at deployment time.  Full point-in-time
    # chronology before this migration cannot be reconstructed honestly.
    op.execute(sa.text("""
        INSERT INTO forecast_currentness_events (
            participant_id, local_date, forecast_id, forecast_version,
            event_type, reason, occurred_at
        )
        SELECT participant_id, local_date, id, forecast_version,
               'activated', 'migration_current_state_seed', CURRENT_TIMESTAMP
        FROM (
            SELECT participant_id, local_date, id, forecast_version,
                   ROW_NUMBER() OVER (
                       PARTITION BY participant_id, local_date
                       ORDER BY generated_at DESC, id DESC
                   ) AS current_rank
            FROM forecast_snapshots
            WHERE valid = true
        ) AS current_forecasts
        WHERE current_rank = 1
    """))


def downgrade() -> None:
    op.drop_index(
        "ix_forecast_currentness_forecast",
        table_name="forecast_currentness_events",
    )
    op.drop_index(
        "ix_forecast_currentness_at",
        table_name="forecast_currentness_events",
    )
    op.drop_table("forecast_currentness_events")
