"""Single source of truth for participant-visible CardAction contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CardActionKind = Literal["navigation", "internal_write", "external_write"]
ReplayPolicy = Literal["safe", "receipt_required"]


@dataclass(frozen=True)
class CardActionSpec:
    name: str
    versions: frozenset[str]
    kind: CardActionKind
    replay_policy: ReplayPolicy


_NAVIGATION = {
    "course_schedule_import_context_open",
    "course_schedule_item_time_open",
    "calendar_mutation_plan_item_time_open",
    "calendar_mutation_plan_item_edit_open",
    "calendar_mutation_plan_view",
    "external_llm_consent_decline",
    "external_llm_consent_details_open",
    "external_llm_consent_prompt_open",
    "external_llm_consent_status_open",
    "feature_back",
    "feature_open",
    "memory_center_refresh",
    "memory_clear_prompt",
    "memory_delete_prompt",
    "memory_detail_open",
    "memory_edit_open",
    "request_checkin",
    "view_calendar_date",
    "view_today_calendar",
}

_INTERNAL_WRITE = {
    "calendar_delete_cancel",
    "calendar_mutation_plan_cancel",
    "calendar_mutation_plan_item_time_submit",
    "calendar_mutation_plan_item_edit_submit",
    "course_schedule_import_context_submit",
    "course_schedule_item_time_submit",
    "daily_review_submit",
    "external_llm_consent_accept",
    "external_llm_consent_revoke",
    "memory_clear_confirm",
    "memory_delete_confirm",
    "memory_edit_save",
    "morning_brief_pause_week",
    "morning_brief_time_update",
    "morning_brief_toggle",
    "preference_settings_save",
    "submit_checkin",
    "support_acknowledge_first",
    "support_followup_disable",
    "care_ack",
    "care_disable_type",
    "care_helpful",
    "care_mute_today",
    "care_not_relevant",
    "care_snooze_30",
}

_EXTERNAL_WRITE = {
    "calendar_delete_confirm",
    "calendar_mutation_plan_confirm",
    "course_schedule_import_cancel",
    "course_schedule_import_confirm",
}


_VERSIONS = {
    "course_schedule_import_confirm": frozenset({"2"}),
    "course_schedule_import_cancel": frozenset({"2"}),
    "course_schedule_import_context_open": frozenset({"3"}),
    "course_schedule_import_context_submit": frozenset({"3"}),
    # v1 remains accepted for already-delivered legacy time cards; builders
    # emit v1 open actions and v2 structured form submissions.
    "course_schedule_item_time_open": frozenset({"1", "2"}),
    "course_schedule_item_time_submit": frozenset({"1", "2"}),
}


CARD_ACTION_REGISTRY: dict[str, CardActionSpec] = {
    name: CardActionSpec(
        name=name,
        versions=_VERSIONS.get(name, frozenset({"1"})),
        kind=kind,
        replay_policy="safe" if kind == "navigation" else "receipt_required",
    )
    for kind, names in (
        ("navigation", _NAVIGATION),
        ("internal_write", _INTERNAL_WRITE),
        ("external_write", _EXTERNAL_WRITE),
    )
    for name in sorted(names)
}


def card_action_spec(name: object) -> CardActionSpec | None:
    return CARD_ACTION_REGISTRY.get(str(name or "").strip())
