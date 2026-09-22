import uuid

from app.integrations.feishu.cards import (
    memory_center_card,
    memory_clear_all_confirmation_card,
    memory_delete_confirmation_card,
    memory_detail_card,
    memory_edit_card,
)
from app.repositories_memory import ParticipantMemoryRepository
from app.services.card_action_service import CardActionService
from app.services.memory_service import MemoryService
from tests.helpers import memory_database, participant


def _actions(value):
    found = []
    if isinstance(value, dict):
        action = value.get("value")
        if isinstance(action, dict) and action.get("mindflow_action"):
            found.append(action["mindflow_action"])
        for child in value.values():
            found.extend(_actions(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_actions(child))
    return found


def test_memory_center_cards_use_fixed_confirmation_actions():
    item = {"id": str(uuid.uuid4()), "memory_type": "goal", "content": "完成论文"}
    assert "memory_detail_open" in _actions(memory_center_card([item]))
    assert "memory_delete_prompt" in _actions(memory_detail_card(item))
    assert "memory_edit_open" in _actions(memory_detail_card(item))
    assert "memory_edit_save" in _actions(memory_edit_card(item))
    assert "memory_delete_confirm" in _actions(memory_delete_confirmation_card(item))
    assert "memory_clear_confirm" in _actions(memory_clear_all_confirmation_card())


def test_memory_center_delete_and_clear_are_participant_bound():
    database = memory_database()
    first = participant(database, "CENTER-1")
    second = participant(database, "CENTER-2")
    memory = MemoryService(ParticipantMemoryRepository(database))
    first_item = memory.remember_explicit(first.id, memory_type="goal", content="完成论文")
    memory.remember_explicit(second.id, memory_type="goal", content="完成项目")
    actions = CardActionService(
        observations=None, observation_refresh=None, memory=memory
    )

    foreign = actions.handle(
        second.id, message_id="m1",
        action_value={"mindflow_action": "memory_delete_confirm", "version": "1", "memory_id": first_item["id"]},
        form_value=None,
    )
    assert foreign == {"ok": False, "error": "memory_not_found"}
    cleared = actions.handle(
        first.id, message_id="m2",
        action_value={"mindflow_action": "memory_clear_confirm", "version": "1"},
        form_value=None,
    )
    assert cleared["ok"] is True
    assert memory.list(first.id) == []
    assert len(memory.list(second.id)) == 1


def test_memory_center_edit_supersedes_old_item_and_is_participant_bound():
    database = memory_database()
    first = participant(database, "CENTER-3")
    second = participant(database, "CENTER-4")
    memory = MemoryService(ParticipantMemoryRepository(database))
    old = memory.remember_explicit(first.id, memory_type="goal", content="完成论文初稿")
    actions = CardActionService(observations=None, observation_refresh=None, memory=memory)

    foreign = actions.handle(
        second.id, message_id="m3",
        action_value={"mindflow_action": "memory_edit_save", "version": "1", "memory_id": old["id"]},
        form_value={"content": "完成论文定稿"},
    )
    edited = actions.handle(
        first.id, message_id="m4",
        action_value={"mindflow_action": "memory_edit_save", "version": "1", "memory_id": old["id"]},
        form_value={"content": "完成论文定稿"},
    )

    assert foreign == {"ok": False, "error": "memory_not_found"}
    assert edited["ok"] is True
    assert [row["content"] for row in memory.list(first.id)] == ["完成论文定稿"]
    assert memory.replace(first.id, uuid.UUID(old["id"]), content="再次修改") is None
