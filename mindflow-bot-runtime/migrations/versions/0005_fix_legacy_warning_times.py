"""Correct warning times backfilled by the forecast hardening migration.

Revision ID: 0005_fix_legacy_warning_times
Revises: 0004_forecast_hardening
"""

from alembic import op


revision = "0005_fix_legacy_warning_times"
down_revision = "0004_forecast_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0004 produced this exact signature for rows created by 0003. New code
    # always stores target_time before risk_time, so the predicate avoids a
    # broad rewrite of normal warning rows.
    op.execute(
        """
        UPDATE warning_schedules
        SET risk_time = target_time + INTERVAL '20 minutes',
            valid_until = target_time + INTERVAL '10 minutes'
        WHERE status IN ('pending', 'claimed', 'delivery_unavailable')
          AND risk_time = target_time
          AND valid_until = target_time + INTERVAL '30 minutes'
        """
    )


def downgrade() -> None:
    # Corrected timestamps cannot be distinguished from legitimate data after
    # subsequent application activity, so downgrade intentionally preserves them.
    pass
