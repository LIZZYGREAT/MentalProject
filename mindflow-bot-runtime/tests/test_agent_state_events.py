"""Participant-scoped backend state events injected into the next Agent turn.

These contracts previously lived in ``test_remaining_work_closure.py``; they are
the only coverage of the append-only agent-state ledger and of the prompt block
that hands those facts to the Agent.
"""

from __future__ import annotations

from types import SimpleNamespace

from app import main as app_main
from app.agent.sdk_adapter import _text_transport_prompt
from app.contracts.agent_input import AgentTurnInput
from app.repositories import ClaudeSessionRepository
from app.repositories_agent_state import ParticipantAgentStateEventRepository
from helpers import memory_database, participant


def test_agent_state_event_is_participant_isolated_and_cursored():
    database = memory_database()
    first = participant(database, "STATE-FIRST")
    second = participant(database, "STATE-SECOND")
    events = ParticipantAgentStateEventRepository(database)
    first_event = events.append(first.id, {
        "event_type": "personalization_proposal_cancelled",
        "resource_kind": "personalization_proposal",
        "resource_id": "proposal-opaque",
        "state": "cancelled",
        "summary": "审核已取消",
    })
    events.append(second.id, {
        "event_type": "reminder_proposal_confirmed",
        "resource_kind": "reminder_proposal",
        "resource_id": "reminder-opaque",
        "state": "confirmed",
        "summary": "提醒已确认",
    })

    assert len(events.list_after(first.id)) == 1

    ClaudeSessionRepository(database).save(
        first.id,
        "session-first",
        last_message_id="m",
        last_backend_state_event_id=first_event["id"],
    )

    assert events.pending_for_turn(first.id) == ((), None)
    assert len(events.pending_for_turn(second.id)[0]) == 1


def test_card_action_state_event_survives_card_update_failure_and_navigation_is_ignored():
    database = memory_database()
    owner = participant(database, "STATE-CARD")
    events = ParticipantAgentStateEventRepository(database)

    class Identity:
        def resolve(self, *_args):
            return owner

    class Actions:
        def __init__(self, navigation=False):
            self.navigation = navigation

        def handle(self, *_args, **_kwargs):
            result = {
                "ok": True,
                "reply_text": "审核已取消",
                "card": {"schema": "2.0"},
                "backend_state_update": {
                    "event_type": "personalization_proposal_cancelled",
                    "resource_kind": "personalization_proposal",
                    "resource_id": "opaque",
                    "state": "cancelled",
                    "summary": "审核已取消",
                },
            }
            if self.navigation:
                result["navigation_only"] = True
            return result

    class Sender:
        def update_card_from_callback(self, *_args):
            raise RuntimeError("card update failed")

    event = SimpleNamespace(
        event_id="state-card-event",
        message_id="state-card-message",
        app_id="app",
        open_id="open",
        chat_id="chat",
        action_tag="button",
        action_value={"mindflow_action": "personalization_proposal_cancel", "version": "1"},
        form_value={},
        callback_token="raw-callback-token",
    )
    handler = app_main._build_card_action_handler(
        Identity(), Actions(), Sender(), backend_state_events=events
    )

    result = handler(event)

    assert result["ok"] is True
    assert events.list_after(owner.id)

    navigation = app_main._build_card_action_handler(
        Identity(), Actions(navigation=True), Sender(), backend_state_events=events
    )
    navigation_event = SimpleNamespace(
        **{**event.__dict__, "event_id": "navigation-event"}
    )

    navigation(navigation_event)

    assert len(events.list_after(owner.id)) == 1


def test_state_prompt_has_no_callback_payload_and_is_not_permission():
    prompt = _text_transport_prompt(AgentTurnInput(
        text="你记得吗？",
        backend_state_updates=({
            "event_type": "personalization_proposal_cancelled",
            "resource_kind": "personalization_proposal",
            "resource_id": "opaque-id",
            "state": "cancelled",
            "summary": "审核已取消",
        },),
    ))

    assert "opaque-id" not in prompt
    assert "raw-callback-token" not in prompt
    assert "callback payload" in prompt
    assert "permissions" in prompt
