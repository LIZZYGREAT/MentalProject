"""Fast schema checks for fixed interactive Feishu card builders."""

from app.card_actions.registry import card_action_spec

from app.integrations.feishu.cards import (
    calendar_delete_confirmation_card,
    calendar_mutation_plan_confirmation_card,
    calendar_mutation_plan_item_time_card,
    care_intervention_card,
    course_schedule_preview_card,
    course_schedule_result_card,
    course_schedule_context_card,
    course_schedule_item_time_card,
    daily_checkin_card,
    daily_review_card,
    external_llm_consent_card,
    external_llm_consent_details_card,
    external_llm_consent_status_card,
    memory_clear_all_confirmation_card,
    memory_center_card,
    memory_delete_confirmation_card,
    memory_detail_card,
    memory_edit_card,
    morning_brief_settings_card,
    preference_settings_card,
    pressure_curve_card,
    today_calendar_card,
)
from app.presentation.feature_cards import (
    feature_detail_card,
    feature_overview_card,
    onboarding_welcome_card,
    visible_feature_keys,
)
from app.services.curve_analysis import analyze_curve


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


def _reviewed_interactive_cards():
    import_id = "00000000-0000-0000-0000-000000000001"
    item_id = "00000000-0000-0000-0000-000000000002"
    context_draft = {
        "id": import_id,
        "status": "pending_context",
        "structured_result": {"missing_context": ["semester_start_date"]},
        "items": [{
            "id": item_id,
            "course_name": "测试课程",
            "start_time": "08:00",
            "end_time": "09:00",
        }],
    }
    confirmation_draft = {
        "id": import_id,
        "status": "pending_confirmation",
        "structured_result": {"courses": [], "missing_context": []},
        "items": [],
    }
    calendar_plan_id = "00000000-0000-0000-0000-000000000004"
    calendar_item_id = "00000000-0000-0000-0000-000000000005"
    calendar_item = {
        "summary": "测试日程",
        "start_time": "2026-09-14T09:00:00+08:00",
        "end_time": "2026-09-14T10:00:00+08:00",
    }
    editable_calendar_plan = {
        "id": calendar_plan_id,
        "operation": "create",
        "status": "awaiting_confirmation",
        "items": [calendar_item],
        "ledger_items": [{
            "id": calendar_item_id,
            "item_index": 0,
            "status": "pending",
            "payload": calendar_item,
        }],
    }
    memory = {
        "id": "00000000-0000-0000-0000-000000000003",
        "memory_type": "preference",
        "content": "测试记忆",
    }
    analysis = analyze_curve([
        {"time": "09:00", "stress_0_10": 4.0, "vitality_0_10": 6.0}
    ])
    feature_cards = {
        "feature_overview": feature_overview_card(),
        "onboarding_welcome": onboarding_welcome_card(),
        **{
            f"feature_detail_{key}": feature_detail_card(key)
            for key in visible_feature_keys()
        },
    }
    return {
        "course_schedule_context": course_schedule_context_card(context_draft),
        "course_schedule_item_time": course_schedule_item_time_card(
            context_draft, item_id
        ),
        "course_schedule_preview_context": course_schedule_preview_card(
            context_draft
        ),
        "course_schedule_preview_confirmation": course_schedule_preview_card(
            confirmation_draft
        ),
        "course_schedule_retry": course_schedule_result_card(
            "请重试",
            status="partial_failed",
            import_id=import_id,
            error="calendar_not_connected",
            recurrence_strategy="preserve_schedule_pattern",
        ),
        "daily_checkin": daily_checkin_card(),
        "daily_review": daily_review_card(
            schedule_id="schedule-1", local_date="2026-09-13"
        ),
        "care_intervention": care_intervention_card(
            intervention_id=import_id,
            message="测试提醒",
            actions=[
                "ack",
                "snooze_30",
                "mute_today",
                "helpful",
                "not_relevant",
                "disable_type",
            ],
        ),
        "calendar_delete": calendar_delete_confirmation_card({
            "id": "event-1",
            "summary": "测试日程",
            "start_time": "2026-09-14T09:00:00+08:00",
            "end_time": "2026-09-14T10:00:00+08:00",
        }),
        "calendar_mutation_plan": calendar_mutation_plan_confirmation_card({
            "id": import_id,
            "operation": "delete",
            "items": [
                {
                    "summary": "一",
                    "start_time": "2026-09-14T09:00:00+08:00",
                    "end_time": "2026-09-14T10:00:00+08:00",
                },
                {
                    "summary": "二",
                    "start_time": "2026-09-14T10:00:00+08:00",
                    "end_time": "2026-09-14T11:00:00+08:00",
                },
            ],
        }),
        "calendar_mutation_plan_item_time": calendar_mutation_plan_item_time_card(
            editable_calendar_plan, calendar_item_id
        ),
        "pressure_curve": pressure_curve_card(
            analysis, image_key="img-key", local_date="2026-09-14"
        ),
        "today_calendar": today_calendar_card([], local_date="2026-09-14"),
        "morning_brief": morning_brief_settings_card({}),
        "preference_settings": preference_settings_card({}),
        "memory_center": memory_center_card([memory]),
        "memory_detail": memory_detail_card(memory),
        "memory_edit": memory_edit_card(memory),
        "memory_delete": memory_delete_confirmation_card(memory),
        "memory_clear": memory_clear_all_confirmation_card(),
        "external_llm_consent": external_llm_consent_card(),
        "external_llm_consent_details": external_llm_consent_details_card(),
        "external_llm_consent_status": external_llm_consent_status_card(
            {"active": False}
        ),
        **feature_cards,
    }


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


def test_all_participant_visible_interactive_cards_match_registry_contract():
    cards = _reviewed_interactive_cards()
    assert len(cards) >= 30
    schema_inventory = {
        name: card.get("schema", "1.0") for name, card in cards.items()
    }
    assert {
        name for name, schema in schema_inventory.items() if schema == "1.0"
    } == {
        "morning_brief",
        "preference_settings",
        "memory_center",
        "memory_detail",
        "memory_edit",
        "memory_delete",
        "memory_clear",
    }
    for name, card in cards.items():
        assert card is not None, name
        assert schema_inventory[name] in {"1.0", "2.0"}, name
        _assert_card_contract(card)


def test_card_2_callback_encoding_inventory_is_explicit():
    inventory = {}
    for name, card in _reviewed_interactive_cards().items():
        encodings = set()
        for element in _walk(card):
            if any(
                isinstance(behavior, dict)
                and behavior.get("type") == "callback"
                for behavior in element.get("behaviors", [])
            ):
                encodings.add("behaviors_callback")
            if (
                element.get("tag") in {"button", "select_static", "input"}
                and
                isinstance(element.get("value"), dict)
                and element["value"].get("mindflow_action")
                and not element.get("behaviors")
            ):
                encodings.add("direct_value")
        inventory[name] = frozenset(encodings)

    assert all(value for value in inventory.values())
    assert inventory["feature_overview"] == frozenset({"behaviors_callback"})
    assert "direct_value" in inventory["memory_center"]
    assert "direct_value" in inventory["preference_settings"]
