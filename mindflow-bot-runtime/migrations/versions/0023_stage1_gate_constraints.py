"""Close Stage-1 runtime-selection and data-integrity gaps.

Revision ID: 0023_stage1_gate_constraints
Revises: 0022_research_profile_v2
"""

from alembic import op


revision = "0023_stage1_gate_constraints"
down_revision = "0022_research_profile_v2"
branch_labels = None
depends_on = None


LEARNED_CONSTRAINTS = (
    (
        "ck_learned_profile_validation_status",
        "validation_status IN ('candidate', 'validated', 'rejected')",
    ),
    ("ck_learned_profile_sample_count", "sample_count >= 0"),
    ("ck_learned_profile_day_count", "day_count >= 0"),
    (
        "ck_learned_profile_confidence",
        "confidence >= 0 AND confidence <= 1",
    ),
    ("ck_learned_profile_window", "window_start <= window_end"),
)

SLOW_STATE_CONSTRAINTS = (
    ("ck_slow_state_cadence", "cadence IN ('daily', 'weekly')"),
    (
        "ck_slow_state_stress",
        "rolling_7d_stress IS NULL OR "
        "(rolling_7d_stress >= 0 AND rolling_7d_stress <= 10)",
    ),
    (
        "ck_slow_state_workload",
        "rolling_7d_workload IS NULL OR "
        "(rolling_7d_workload >= 0 AND rolling_7d_workload <= 10)",
    ),
    (
        "ck_slow_state_energy",
        "rolling_7d_energy IS NULL OR "
        "(rolling_7d_energy >= 0 AND rolling_7d_energy <= 10)",
    ),
    (
        "ck_slow_state_recovery",
        "recent_recovery_quality IS NULL OR "
        "(recent_recovery_quality >= 0 AND recent_recovery_quality <= 10)",
    ),
    (
        "ck_slow_state_sleep_debt",
        "recent_sleep_debt IS NULL OR "
        "(recent_sleep_debt >= 0 AND recent_sleep_debt <= 24)",
    ),
)


def upgrade() -> None:
    # Existing pilot rows created before 0022 retain model_version='legacy'.
    # No row is relabeled as validated; the application preserves those rows
    # through an explicit runtime compatibility rule.
    for name, condition in LEARNED_CONSTRAINTS:
        op.create_check_constraint(name, "learned_model_profiles", condition)
    for name, condition in SLOW_STATE_CONSTRAINTS:
        op.create_check_constraint(name, "participant_slow_states", condition)


def downgrade() -> None:
    for name, _condition in reversed(SLOW_STATE_CONSTRAINTS):
        op.drop_constraint(name, "participant_slow_states", type_="check")
    for name, _condition in reversed(LEARNED_CONSTRAINTS):
        op.drop_constraint(name, "learned_model_profiles", type_="check")

