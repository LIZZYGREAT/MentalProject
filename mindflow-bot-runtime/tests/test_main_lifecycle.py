import asyncio
from types import SimpleNamespace

import pytest

from app import main as app_main


def test_daily_review_scheduler_fails_closed_without_card_action_transport():
    disabled = SimpleNamespace(daily_review_enabled=False)
    enabled = SimpleNamespace(daily_review_enabled=True)

    assert app_main._should_start_daily_review_scheduler(disabled, True) is False
    assert app_main._should_start_daily_review_scheduler(enabled, False) is False
    assert app_main._should_start_daily_review_scheduler(enabled, True) is True


def _transport_settings(transport, callback_enabled=False):
    return SimpleNamespace(
        feishu_card_action_transport=transport,
        feishu_card_callback_enabled=callback_enabled,
        feishu_bot_app_id="app",
        feishu_card_verification_token="token",
        feishu_card_encrypt_key="key",
        feishu_card_callback_host="127.0.0.1",
        feishu_card_callback_port=8123,
        feishu_card_callback_path="/callback",
    )


def test_ws_transport_does_not_start_http_callback():
    created = []
    callback = app_main._build_card_callback(
        _transport_settings("ws"),
        object(),
        server_factory=lambda **kwargs: created.append(kwargs),
    )
    assert callback is None
    assert created == []


def test_http_transport_preserves_existing_callback():
    sentinel = object()
    callback = app_main._build_card_callback(
        _transport_settings("http", callback_enabled=True),
        object(),
        server_factory=lambda **_kwargs: sentinel,
    )
    assert callback is sentinel


def test_ws_transport_enables_care_cards():
    assert app_main._card_action_transport_available(
        _transport_settings("ws"), None
    ) is True


def test_ws_transport_enables_daily_review_scheduler():
    settings = _transport_settings("ws")
    settings.daily_review_enabled = True
    available = app_main._card_action_transport_available(settings, None)
    assert app_main._should_start_daily_review_scheduler(settings, available) is True


def _card_action_event():
    return SimpleNamespace(
        event_id="provider-event",
        message_id="om-card",
        app_id="app",
        open_id="ou-user",
        chat_id="oc-chat",
        action_tag="button",
        action_value={"mindflow_action": "submit_checkin"},
        form_value={},
    )


def test_card_action_handler_updates_original_card_after_success():
    participant = SimpleNamespace(id="participant-1")

    class CardActions:
        def __init__(self):
            self.calls = 0

        def handle(self, participant_id, **kwargs):
            self.calls += 1
            assert participant_id == participant.id
            assert kwargs["callback_event_id"] == "provider-event"
            return {"ok": True, "reply_text": "已记录"}

    class Sender:
        def __init__(self):
            self.updated = []

        def update_card(self, message_id, card):
            self.updated.append((message_id, card))

    sender = Sender()
    card_actions = CardActions()
    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: participant), card_actions, sender
    )
    result = handler(_card_action_event())
    assert result["ok"] is True
    assert result["card_update_ok"] is True
    assert card_actions.calls == 1
    assert sender.updated == [("om-card", result["card"])]


def test_card_action_handler_keeps_success_when_card_update_fails_after_commit():
    participant = SimpleNamespace(id="participant-1")

    class CardActions:
        def __init__(self):
            self.calls = 0

        def handle(self, participant_id, **_kwargs):
            self.calls += 1
            assert participant_id == participant.id
            return {"ok": True, "reply_text": "已记录"}

    class Sender:
        def __init__(self):
            self.update_calls = 0
            self.messages = []

        def update_card(self, _message_id, _card):
            self.update_calls += 1
            raise RuntimeError("card patch failed")

        def send_text(self, chat_id, text):
            self.messages.append((chat_id, text))

    class Incidents:
        def __init__(self):
            self.records = []

        def record(self, **kwargs):
            self.records.append(kwargs)

    sender = Sender()
    card_actions = CardActions()
    incidents = Incidents()
    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: participant),
        card_actions,
        sender,
        incidents,
    )

    result = handler(_card_action_event())

    assert result["ok"] is True
    assert result["card_update_ok"] is False
    assert card_actions.calls == 1
    assert sender.update_calls == 1
    assert sender.messages == [
        (
            "oc-chat",
            "操作已记录，但卡片状态暂未更新，无需重复提交。",
        )
    ]
    assert incidents.records[0]["event_name"] == (
        "card_action_card_update_failed_after_commit"
    )
    assert incidents.records[0]["error_code"] == "card_update_failed_after_commit"


def test_card_action_handler_preserves_business_failure_behavior():
    participant = SimpleNamespace(id="participant-1")

    class CardActions:
        def __init__(self):
            self.calls = 0

        def handle(self, _participant_id, **_kwargs):
            self.calls += 1
            raise ValueError("business failed")

    class Sender:
        def __init__(self):
            self.update_calls = 0
            self.messages = []

        def update_card(self, _message_id, _card):
            self.update_calls += 1

        def send_text(self, chat_id, text):
            self.messages.append((chat_id, text))

    class Incidents:
        def __init__(self):
            self.records = []

        def record(self, **kwargs):
            self.records.append(kwargs)

    sender = Sender()
    card_actions = CardActions()
    incidents = Incidents()
    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: participant),
        card_actions,
        sender,
        incidents,
    )

    with pytest.raises(ValueError, match="business failed"):
        handler(_card_action_event())

    assert card_actions.calls == 1
    assert sender.update_calls == 0
    assert sender.messages == [("oc-chat", "操作未能完成，请稍后重试。")]
    assert incidents.records[0]["event_name"] == "card_action_failed"
    assert incidents.records[0]["error_code"] == "business_failed"


def test_sigterm_during_gateway_start_still_cancels_start(monkeypatch):
    class SlowGateway:
        def __init__(self):
            self.start_cancelled = False

        async def start(self):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.start_cancelled = True
                raise

    gateway = SlowGateway()

    async def scenario():
        loop = asyncio.get_running_loop()

        def trigger(_sig, callback):
            loop.call_soon(callback)

        monkeypatch.setattr(loop, "add_signal_handler", trigger)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: None)
        await asyncio.wait_for(
            app_main._run_gateway_until_shutdown(gateway), timeout=1
        )
        assert gateway.start_cancelled

    asyncio.run(scenario())


def test_scheduler_ready_callback_runs_only_after_gateway_start(monkeypatch):
    order = []

    class Gateway:
        async def start(self):
            order.append("gateway_ready")

        async def wait_closed(self):
            order.append("gateway_wait")

    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda *_args: None)

        async def start_scheduler():
            order.append("scheduler_ready")

        await app_main._run_gateway_until_shutdown(
            Gateway(), on_ready=start_scheduler
        )

    asyncio.run(scenario())
    assert order == ["gateway_ready", "scheduler_ready", "gateway_wait"]
