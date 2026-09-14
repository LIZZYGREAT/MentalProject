from types import SimpleNamespace

import pytest

from app.card_actions.delivery import card_update_failure_policy
from app.card_actions.registry import CARD_ACTION_REGISTRY, card_action_spec
from app.integrations.feishu.client import FeishuSendError


def test_registry_covers_action_families_and_failure_semantics():
    assert card_action_spec("feature_back").kind == "navigation"
    assert card_action_spec("memory_detail_open").replay_policy == "safe"
    assert card_action_spec("support_followup_disable").kind == "internal_write"
    assert card_action_spec("calendar_delete_confirm").kind == "external_write"
    assert card_action_spec("course_schedule_import_confirm").replay_policy == (
        "receipt_required"
    )
    assert card_action_spec("course_schedule_import_confirm").versions == frozenset({"2"})
    assert card_action_spec("course_schedule_item_time_submit").versions == frozenset(
        {"1", "2"}
    )
    assert card_action_spec("calendar_mutation_plan_item_time_open").kind == (
        "navigation"
    )
    assert card_action_spec("calendar_mutation_plan_view").replay_policy == "safe"
    assert card_action_spec("calendar_mutation_plan_item_time_submit").kind == (
        "internal_write"
    )
    assert card_action_spec(
        "calendar_mutation_plan_item_time_submit"
    ).replay_policy == "receipt_required"
    assert all(spec.versions for spec in CARD_ACTION_REGISTRY.values())


@pytest.mark.parametrize(
    ("error", "code", "replacement_allowed"),
    [
        (FeishuSendError("gone", code=230003, replacement_allowed=True), 230003, True),
        (FeishuSendError("recalled", code=230006, replacement_allowed=True), 230006, True),
        (FeishuSendError("denied", code=230001), 230001, False),
        (FeishuSendError("auth", code=99991672), 99991672, False),
        (RuntimeError("unknown"), None, False),
    ],
)
def test_update_failure_policy_defaults_to_no_replacement(
    error, code, replacement_allowed
):
    policy = card_update_failure_policy(error)
    assert policy.provider_error_code == code
    assert policy.replacement_allowed is replacement_allowed


def test_update_failure_policy_preserves_operation_and_retryability():
    policy = card_update_failure_policy(
        FeishuSendError("temporary", retryable=True, operation="update_card")
    )
    assert policy.retryable is True
    assert policy.operation == "update_card"
