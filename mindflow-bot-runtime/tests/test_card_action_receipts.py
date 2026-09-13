from types import SimpleNamespace

import pytest

from app import main as app_main
from app.models import CardActionReceipt
from app.repositories_card_action import CardActionReceiptRepository
from tests.helpers import memory_database, participant


def _event(*, event_id="card-event-1", action="submit_checkin"):
    return SimpleNamespace(
        event_id=event_id,
        message_id="om-card",
        app_id="app",
        open_id="ou-user",
        chat_id="oc-chat",
        action_tag="button",
        action_value={"mindflow_action": action, "version": "1"},
        form_value={"stress": "7"},
        callback_token="must-not-be-persisted",
    )


def test_duplicate_success_replays_card_without_repeating_business_write():
    database = memory_database()
    bound = participant(database, "CARDREPLAY1")
    business_calls = []
    delivered = []

    class Sender:
        def update_card_from_callback(self, token, message_id, card):
            delivered.append((token, message_id, card))

    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: bound),
        SimpleNamespace(
            handle=lambda *_args, **_kwargs: (
                business_calls.append("write")
                or {
                    "ok": True,
                    "reply_text": "已记录",
                    "card": {"schema": "2.0", "body": {"elements": []}},
                }
            )
        ),
        Sender(),
        receipts=CardActionReceiptRepository(database),
    )

    first = handler(_event())
    replay = handler(_event())

    assert first["ok"] is True
    assert replay["ok"] is True
    assert replay["receipt_replayed"] is True
    assert business_calls == ["write"]
    assert len(delivered) == 2
    with database.session() as session:
        receipt = session.get(CardActionReceipt, "card-event-1")
        assert receipt.status == "succeeded"
        assert receipt.result_kind == "mutation"
        persisted = " ".join(
            str(value)
            for value in (
                receipt.action_name,
                receipt.action_fingerprint,
                receipt.message_id_hash,
                receipt.result_json,
            )
        )
        assert "must-not-be-persisted" not in persisted


def test_reused_event_id_with_different_action_is_rejected():
    database = memory_database()
    bound = participant(database, "CARDCONFLICT1")
    calls = []
    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: bound),
        SimpleNamespace(
            handle=lambda *_args, **_kwargs: (
                calls.append("write")
                or {"ok": True, "reply_text": "ok", "card": {"schema": "2.0"}}
            )
        ),
        None,
        receipts=CardActionReceiptRepository(database),
    )

    handler(_event())
    with pytest.raises(PermissionError, match="identity conflict"):
        handler(_event(action="memory_clear_confirm"))
    assert calls == ["write"]


def test_failed_receipt_fails_closed_without_reexecution():
    database = memory_database()
    bound = participant(database, "CARDPROCESS1")
    receipts = CardActionReceiptRepository(database)
    event = _event()
    calls = []

    class Actions:
        def handle(self, *_args, **_kwargs):
            calls.append("write")
            raise RuntimeError("leave no successful result")

    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: bound),
        Actions(),
        None,
        receipts=receipts,
    )
    with pytest.raises(RuntimeError):
        handler(event)
    result = handler(event)
    assert result["ok"] is False
    assert result["receipt_replayed"] is True
    assert calls == ["write"]


def test_processing_receipt_returns_in_progress_without_business_execution():
    database = memory_database()
    bound = participant(database, "CARDPROCESS2")
    receipts = CardActionReceiptRepository(database)
    receipts.claim(
        event_id="processing-event",
        participant_id=bound.id,
        action_name="submit_checkin",
        action_version="1",
        action_fingerprint="fingerprint",
        message_id_hash="message-hash",
    )
    replay = receipts.claim(
        event_id="processing-event",
        participant_id=bound.id,
        action_name="submit_checkin",
        action_version="1",
        action_fingerprint="fingerprint",
        message_id_hash="message-hash",
    )
    assert replay.outcome == "replay"
    assert replay.status == "processing"
