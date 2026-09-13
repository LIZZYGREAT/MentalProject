"""Fast schema checks for fixed interactive Feishu card builders."""

from app.card_actions.registry import card_action_spec

from app.integrations.feishu.cards import (
    course_schedule_context_card,
    course_schedule_item_time_card,
    daily_checkin_card,
    daily_review_card,
    external_llm_consent_card,
    external_llm_consent_details_card,
    external_llm_consent_status_card,
    memory_clear_all_confirmation_card,
    memory_delete_confirmation_card,
    memory_edit_card,
    morning_brief_settings_card,
    preference_settings_card,
)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _assert_callback_contract(element):
    callback_values = [
        behavior.get("value")
        for behavior in element.get("behaviors", [])
        if behavior.get("type") == "callback"
    ]
    if "value" in element:
        callback_values.append(element["value"])
    for value in callback_values:
        assert isinstance(value, dict)
        action_name = str(value.get("mindflow_action") or "").strip()
        version = str(value.get("version") or "").strip()
        assert action_name
        assert version
        spec = card_action_spec(action_name)
        assert spec is not None, action_name
        assert version in spec.versions, (action_name, version)


def _assert_card_contract(card):
    for element in _walk(card):
        if element.get("tag") in {"button", "select_static", "input"}:
            _assert_callback_contract(element)
        if element.get("tag") != "form":
            continue
        assert str(element.get("name") or "").strip()
        for child in _walk(element.get("elements", [])):
            if child.get("tag") in {"button", "select_static", "input"}:
                assert str(child.get("name") or "").strip()
            if child.get("tag") == "button" and (
                "form_action_type" in child or "action_type" in child
            ):
                assert (
                    child.get("form_action_type") == "submit"
                    or child.get("action_type") == "form_submit"
                )


def test_reviewed_fixed_cards_satisfy_interactive_contract():
    draft = {
        "id": "00000000-0000-0000-0000-000000000001",
        "structured_result": {"missing_context": ["semester_start_date"]},
        "items": [{
            "id": "00000000-0000-0000-0000-000000000002",
            "course_name": "测试课程",
            "start_time": "08:00",
            "end_time": "09:00",
        }],
    }
    memory = {
        "id": "00000000-0000-0000-0000-000000000003",
        "memory_type": "preference",
        "content": "测试记忆",
    }
    cards = (
        daily_checkin_card(),
        daily_review_card(schedule_id="schedule-1", local_date="2026-09-13"),
        morning_brief_settings_card({}),
        course_schedule_context_card(draft),
        course_schedule_item_time_card(draft, draft["items"][0]["id"]),
        preference_settings_card({}),
        memory_edit_card(memory),
        memory_delete_confirmation_card(memory),
        memory_clear_all_confirmation_card(),
        external_llm_consent_card(),
        external_llm_consent_details_card(),
        external_llm_consent_status_card({"active": False}),
    )
    for card in cards:
        _assert_card_contract(card)
