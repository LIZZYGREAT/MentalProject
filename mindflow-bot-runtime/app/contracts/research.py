"""Stage-1 research contracts shared by persistence and administration layers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


PROFILE_SCHEMA_VERSION = "2.0"
SUPPORTED_PSYCHOMETRIC_INSTRUMENTS = frozenset({"PSS", "BRS"})
EXPLICIT_PROFILE_FIELDS = frozenset(
    {
        "preferred_name",
        "typical_sleep_window",
        "chronotype",
        "typical_study_load",
        "exercise_frequency",
        "preferred_recovery_methods",
    }
)
SLOW_STATE_FIELDS = (
    "rolling_7d_stress",
    "rolling_7d_workload",
    "rolling_7d_energy",
    "recent_recovery_quality",
    "recent_sleep_debt",
    "exam_period_flag",
)
EVENT_APPRAISAL_SCORE_FIELDS = (
    "mental_demand",
    "physical_demand",
    "temporal_demand",
    "effort",
    "frustration",
    "perceived_control",
    "actual_stress",
    "perceived_performance",
)

# This taxonomy is intentionally small. It classifies evidence by temporal
# meaning; it does not reinterpret Daily Review as an instant observation.
OBSERVATION_TAXONOMY = {
    "momentary_state": {
        "storage": "state_observations",
        "time_field": "observed_at",
        "knowledge_time_field": "created_at",
        "types": ("checkin",),
    },
    "retrospective_state": {
        "storage": "daily_review_responses",
        "time_field": "local_date",
        "knowledge_time_field": "submitted_at",
        "types": ("daily_review",),
    },
    "stable_profile": {
        "storage": "participant_profiles, psychometric_assessments",
        "time_field": "updated_at or administered_at",
        "knowledge_time_field": "created_at",
        "types": ("explicit_profile", "psychometric_assessment"),
    },
    "slow_state": {
        "storage": "participant_slow_states",
        "time_field": "effective_at",
        "knowledge_time_field": "created_at",
        "types": ("daily", "weekly"),
    },
    "learned_parameters": {
        "storage": "learned_model_profiles",
        "time_field": "window_end",
        "knowledge_time_field": "created_at",
        "types": ("validated", "candidate", "rejected"),
    },
}


def aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def score_0_10(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number from 0 to 10")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number from 0 to 10") from exc
    if not 0.0 <= score <= 10.0:
        raise ValueError(f"{field_name} must be between 0 and 10")
    return score


def validate_profile_v2(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Schema v2 while preserving explicitly supported legacy rows."""

    value = dict(profile)
    version = value.get("schema_version")
    if version is None:
        return value
    if str(version) != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"unsupported profile schema_version: {version}")
    explicit = value.get("explicit")
    if not isinstance(explicit, Mapping):
        raise ValueError("Profile Schema v2 requires an explicit object")
    unknown = set(explicit) - EXPLICIT_PROFILE_FIELDS
    if unknown:
        raise ValueError(f"unsupported explicit profile fields: {sorted(unknown)}")
    normalized: dict[str, dict[str, Any]] = {}
    for name, item in explicit.items():
        if not isinstance(item, Mapping):
            raise ValueError(f"explicit.{name} must contain value/source/updated_at")
        missing = {"value", "source", "updated_at"} - set(item)
        if missing:
            raise ValueError(f"explicit.{name} is missing: {sorted(missing)}")
        source = str(item.get("source") or "").strip()
        updated_at = str(item.get("updated_at") or "").strip()
        if not source or not updated_at:
            raise ValueError(f"explicit.{name} source and updated_at are required")
        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"explicit.{name}.updated_at must be ISO 8601") from exc
        aware_utc(parsed, f"explicit.{name}.updated_at")
        normalized[name] = {
            "value": item.get("value"),
            "source": source[:64],
            "updated_at": updated_at,
        }
    value["schema_version"] = PROFILE_SCHEMA_VERSION
    value["explicit"] = normalized
    return value


def normalize_instrument_name(value: Any) -> str:
    name = str(value or "").strip().upper()
    if name not in SUPPORTED_PSYCHOMETRIC_INSTRUMENTS:
        supported = ", ".join(sorted(SUPPORTED_PSYCHOMETRIC_INSTRUMENTS))
        raise ValueError(f"instrument_name must be one of: {supported}")
    return name

