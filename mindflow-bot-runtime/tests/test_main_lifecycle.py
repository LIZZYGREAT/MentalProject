import asyncio
import inspect
from types import SimpleNamespace

import pytest

from app import main as app_main
from app.integrations.feishu.client import FeishuClient, FeishuSendError


def test_web_search_startup_diagnostic_logs_only_configuration_booleans(caplog):
    settings = SimpleNamespace(
        web_search_enabled=True,
        web_search_provider="deepseek_native",
        web_search_model="deepseek-v4-flash",
        deepseek_api_key="secret-key-value",
    )

    with caplog.at_level("INFO"):
        app_main._log_web_search_config(settings)

    assert (
        "web_search_config enabled=True provider=deepseek_native "
        "model=deepseek-v4-flash key_configured=True"
    ) in caplog.text
    assert "secret-key-value" not in caplog.text


def test_streaming_capability_preflight_logs_supported_sender(caplog):
    settings = SimpleNamespace(feishu_streaming_card_enabled=True)
    sender = SimpleNamespace(start_streaming_card=lambda *_args, **_kwargs: None)
    incidents = SimpleNamespace(record=lambda **_kwargs: pytest.fail("unexpected"))

    with caplog.at_level("INFO"):
        app_main._record_streaming_capability(settings, sender, incidents)

    assert (
        "feishu_streaming_capability enabled=True sender_supported=True"
        in caplog.text
    )


def test_streaming_capability_preflight_records_enabled_but_unsupported(caplog):
    records = []
    settings = SimpleNamespace(feishu_streaming_card_enabled=True)
    sender = SimpleNamespace()
    incidents = SimpleNamespace(record=lambda **values: records.append(values))

    with caplog.at_level("INFO"):
        app_main._record_streaming_capability(settings, sender, incidents)

    assert "enabled=True sender_supported=False" in caplog.text
    assert records == [{
        "severity": "warning",
        "subsystem": "feishu",
        "event_name": "feishu_streaming_capability_unavailable",
        "summary": (
            "Streaming is enabled but the configured sender has no CardKit capability."
        ),
        "details": {"enabled": True, "sender_supported": False},
    }]


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
        callback_token="callback-token",
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


def test_card_action_executor_never_calls_delivery_transport():
    participant = SimpleNamespace(id="participant-1")

    executor = app_main._build_card_action_executor(
        SimpleNamespace(resolve=lambda *_args: participant),
        SimpleNamespace(
            handle=lambda *_args, **_kwargs: {
                "ok": True,
                "reply_text": "已完成",
                "card": {"schema": "2.0"},
            }
        ),
    )
    assert "sender" not in inspect.signature(
        app_main._build_card_action_executor
    ).parameters
    assert executor(_card_action_event()) == {
        "ok": True,
        "reply_text": "已完成",
        "card": {"schema": "2.0"},
    }


def test_card_action_handler_uses_callback_token_for_single_delayed_update():
    participant = SimpleNamespace(id="participant-1")

    class Sender:
        def __init__(self):
            self.delayed = []

        def update_card_from_callback(self, token, message_id, card):
            self.delayed.append((token, message_id, card))

        def update_card(self, _message_id, _card):
            raise AssertionError("direct message patch must not also run")

    sender = Sender()
    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: participant),
        SimpleNamespace(
            handle=lambda *_args, **_kwargs: {
                "ok": True,
                "reply_text": "已记录",
                "card": {"schema": "2.0"},
            }
        ),
        sender,
    )

    result = handler(_card_action_event())

    assert result["card_update_ok"] is True
    assert sender.delayed == [
        ("callback-token", "om-card", {"schema": "2.0"})
    ]


def test_card_action_handler_falls_back_after_callback_300090_without_repeating_business_effect():
    participant = SimpleNamespace(id="participant-1")
    requests = []
    patched = []

    class Messages:
        def patch(self, request):
            patched.append(request.message_id)
            return SimpleNamespace(success=lambda: True)

    class SdkClient:
        im = SimpleNamespace(v1=SimpleNamespace(message=Messages()))

        def request(self, request):
            requests.append(request)
            return SimpleNamespace(
                success=lambda: False,
                code=300090,
                msg="callback token target not found",
            )

    class CardActions:
        def __init__(self):
            self.calls = 0

        def handle(self, _participant_id, **_kwargs):
            self.calls += 1
            return {"ok": True, "reply_text": "已记录"}

    sender = FeishuClient("app", "secret", sdk_client=SdkClient())
    card_actions = CardActions()
    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: participant),
        card_actions,
        sender,
    )

    result = handler(_card_action_event())

    assert result["card_update_ok"] is True
    assert card_actions.calls == 1
    assert len(requests) == 1
    assert patched == ["om-card"]


def test_card_action_handler_reports_after_commit_when_callback_fallback_patch_fails():
    participant = SimpleNamespace(id="participant-1")

    class Messages:
        def patch(self, _request):
            return SimpleNamespace(
                success=lambda: False,
                code=230001,
                msg="message patch rejected",
            )

    class SdkClient:
        im = SimpleNamespace(v1=SimpleNamespace(message=Messages()))

        def request(self, _request):
            return SimpleNamespace(
                success=lambda: False,
                code=300090,
                msg="callback token target not found",
            )

    class CardActions:
        def __init__(self):
            self.calls = 0

        def handle(self, _participant_id, **_kwargs):
            self.calls += 1
            return {"ok": True, "reply_text": "已记录"}

    class Incidents:
        def __init__(self):
            self.records = []

        def record(self, **kwargs):
            self.records.append(kwargs)

    sender = FeishuClient("app", "secret", sdk_client=SdkClient())
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
    assert incidents.records[0]["bot_event_id"] is None
    assert incidents.records[0]["details"]["callback_event_id"] == "provider-event"


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
            "操作已经完成，但卡片状态暂未更新，无需重复点击。",
        )
    ]
    assert incidents.records[0]["event_name"] == (
        "card_action_update_failed_after_commit"
    )
    assert incidents.records[0]["error_code"] == "card_update_failed_after_commit"


def test_card_action_handler_uses_idempotent_replacement_card_after_patch_failure():
    participant = SimpleNamespace(id="participant-1")

    class CardActions:
        def __init__(self):
            self.calls = 0

        def handle(self, participant_id, **_kwargs):
            self.calls += 1
            assert participant_id == participant.id
            return {
                "ok": True,
                "navigation_only": True,
                "reply_text": "已打开",
                "card": {"schema": "2.0"},
            }

    class Sender:
        def __init__(self):
            self.cards = []
            self.messages = []

        def update_card(self, _message_id, _card):
            raise FeishuSendError(
                "card patch failed",
                operation="update_card",
                replacement_allowed=True,
            )

        def send_card(self, chat_id, card, *, message_uuid=None):
            self.cards.append((chat_id, card, message_uuid))

        def send_text(self, chat_id, text):
            self.messages.append((chat_id, text))

    sender = Sender()
    card_actions = CardActions()
    handler = app_main._build_card_action_handler(
        SimpleNamespace(resolve=lambda *_args: participant), card_actions, sender
    )

    first = handler(_card_action_event())
    second = handler(_card_action_event())

    assert first["card_update_ok"] is False
    assert first["card_replacement_ok"] is True
    assert second["card_replacement_ok"] is True
    assert card_actions.calls == 2
    assert len(sender.cards) == 2
    assert sender.cards[0][2] == sender.cards[1][2]
    assert sender.cards[0][2]
    assert len(sender.cards[0][2]) <= 50
    assert sender.messages == []


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
