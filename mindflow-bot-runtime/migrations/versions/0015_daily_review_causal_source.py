"""Persist the exact Forecast source accepted with a Daily Review response.

Revision ID: 0015_daily_review_causal_source
Revises: 0014_daily_review_expiry
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_daily_review_causal_source"
down_revision = "0014_daily_review_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_review_responses",
        sa.Column("causal_source_forecast_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "daily_review_responses",
        sa.Column(
            "causal_source_forecast_version", sa.String(64), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_daily_review_response_causal_source_forecast",
        "daily_review_responses",
        "forecast_snapshots",
        ["causal_source_forecast_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        WITH first_reconstruction AS (
            SELECT DISTINCT ON (daily_review_response_id)
                daily_review_response_id,
                source_forecast_id,
                source_forecast_version
            FROM retrospective_curve_snapshots
            ORDER BY daily_review_response_id, generated_at ASC, id ASC
        )
        UPDATE daily_review_responses AS response
        SET causal_source_forecast_id = reconstruction.source_forecast_id,
            causal_source_forecast_version = reconstruction.source_forecast_version
        FROM first_reconstruction AS reconstruction
        WHERE response.id = reconstruction.daily_review_response_id
          AND response.causal_source_forecast_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_daily_review_response_causal_source_forecast",
        "daily_review_responses",
        type_="foreignkey",
    )
    op.drop_column(
        "daily_review_responses", "causal_source_forecast_version"
    )
    op.drop_column("daily_review_responses", "causal_source_forecast_id")
