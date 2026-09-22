"""Scope and audit administrator access to participant research detail.

Revision ID: 0066_admin_research_detail_scope
Revises: 0065_research_aggregate_query_audit
"""

from alembic import op
import sqlalchemy as sa


revision = "0066_admin_research_detail_scope"
down_revision = "0065_research_aggregate_query_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column("scopes_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.execute(
        "UPDATE admin_users SET scopes_json = "
        "'[\"participant_research_detail_read\"]' WHERE role = 'superadmin'"
    )
    op.create_table(
        "admin_research_detail_access_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("admin_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admin_users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_research_detail_access", "admin_research_detail_access_audit",
        ["admin_id", "created_at"], unique=False,
    )
    op.create_index(
        "ix_participant_research_detail_access", "admin_research_detail_access_audit",
        ["participant_id", "created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_participant_research_detail_access",
        table_name="admin_research_detail_access_audit",
    )
    op.drop_index(
        "ix_admin_research_detail_access",
        table_name="admin_research_detail_access_audit",
    )
    op.drop_table("admin_research_detail_access_audit")
    op.drop_column("admin_users", "scopes_json")
