import asyncio
from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.agent.context import AgentContext
from app.agent.skill_loader import SkillLoader
from app.integrations.feishu.calendar import CalendarService, build_recurrence_rule
from app.tools.care import _recurrence_from_args
from app.integrations.feishu.cards import (
    care_intervention_card,
    care_intervention_result_card,
    daily_checkin_card,
    daily_review_card,
    pressure_curve_card,
    today_calendar_card,
)
from app.integrations.feishu.client import FeishuClient, FeishuSendError
from app.integrations.feishu.card_callback import FeishuCardCallbackServer
from app.integrations.feishu.gateway import (
    BotEvent,
    CardActionEvent,
    FeishuCardActionAdapter,
    FeishuGateway,
)
from app.identity.service import IdentityService
from app.repositories import (
    AgentRunRepository,
    BindingRepository,
    BotEventRepository,
    ObservationRepository,
)
from app.services.card_action_service import CardActionService
from app.services.curve_analysis import analyze_curve
from app.services.presentation_service import PendingImageCard, PresentationOutbox
from app.tools.care import CareTools
from app.worker import BotWorker
from helpers import memory_database, participant, skill_path


TZ = ZoneInfo("Asia/Shanghai")


def test_pressure_curve_card_contains_python_image_key_nodes_and_actions():
    analysis = analyze_curve(
        [
            {"time": "09:00", "stress_0_10": 4.5, "vitality_0_10": 7.0},
            {"time": "10:00", "stress_0_10": 8.0, "vitality_0_10": 5.0},
        ]
    )
    card = pressure_curve_card(
        analysis,
        image_key="img-key",
        local_date="2030-01-15",
    )

    assert card["schema"] == "2.0"
    image = card["body"]["elements"][0]
    assert image == {
        "tag": "img",
        "img_key": "img-key",
        "alt": {"tag": "plain_text", "content": "今日压力趋势"},
        "mode": "fit_horizontal",
        "preview": True,
    }
    assert not any(item["tag"] == "chart" for item in card["body"]["elements"])
    summary = card["body"]["elements"][1]["content"]
    assert "今日峰值" in summary
    assert "stress-ctssm" not in summary
    assert "M0" not in summary
    assert "当前精力" not in summary
    actions = {
        item.get("behaviors", [{}])[0].get("value", {}).get("mindflow_action")
        for item in card["body"]["elements"]
        if item.get("tag") == "button"
    }
    assert actions == {"request_checkin", "view_today_calendar"}


def test_daily_checkin_card_uses_json_2_form_submit_contract():
    card = daily_checkin_card()
    assert card["schema"] == "2.0"
    assert card["config"]["update_multi"] is True
    assert "elements" not in card
    form = next(
        item for item in card["body"]["elements"] if item["tag"] == "form"
    )
    assert form["name"] == "mindflow_daily_checkin"
    form_elements = form["elements"]
    fields = {item.get("name"): item for item in form_elements if item.get("name")}

    assert set(fields) == {
        "stress",
        "energy",
        "activity",
        "stress_event_since_last",
        "event_ongoing",
        "submit_checkin",
    }
    select_prompts = {
        "stress": "当前压力",
        "energy": "当前精力",
        "stress_event_since_last": "上次记录后有压力事件吗",
        "event_ongoing": "该事件仍在持续吗",
    }
    for name, prompt in select_prompts.items():
        select = fields[name]
        assert select["tag"] == "select_static"
        assert "label" not in select
        visible_description = form_elements[form_elements.index(select) - 1]
        assert visible_description["tag"] == "markdown"
        assert prompt in visible_description["content"]
    assert fields["activity"]["label"]["content"] == "正在做什么"
    button = fields["submit_checkin"]
    assert button["form_action_type"] == "submit"
    assert "action_type" not in button
    assert button["behaviors"] == [{
        "type": "callback",
        "value": {
        "mindflow_action": "submit_checkin",
        "version": "1",
        },
    }]


def test_card_callback_server_exposes_only_configured_callback_and_health_routes():
    handled = []
    server = FeishuCardCallbackServer(
        app_id="app",
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        action_handler=lambda event: (
            handled.append(event)
            or {"ok": True, "reply_text": "记录成功"}
        ),
        host="127.0.0.1",
        port=8123,
        path="/feishu/card/callback",
    )

    assert {route.path for route in server.app.routes} == {
        "/feishu/card/callback",
        "/healthz",
    }

    async def verify_url_challenge():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://callback.test"
        ) as client:
            response = await client.post(
                "/feishu/card/callback",
                json={
                    "type": "url_verification",
                    "token": "verification-token",
                    "challenge": "challenge-value",
                },
            )
        assert response.status_code == 200
        assert response.json() == {"challenge": "challenge-value"}

        async with httpx.AsyncClient(
            transport=transport, base_url="https://callback.test"
        ) as client:
            callback_body = json.dumps(
                {
                    "schema": "2.0",
                    "header": {
                        "event_id": "event-card-1",
                        "event_type": "card.action.trigger",
                        "token": "verification-token",
                        "app_id": "app",
                        "tenant_key": "tenant",
                    },
                    "event": {
                        "operator": {"open_id": "ou-user"},
                        "token": "update-token",
                        "action": {
                            "tag": "button",
                            "value": {
                                "mindflow_action": "submit_checkin",
                                "version": "1",
                            },
                            "form_value": {"stress": "7"},
                        },
                        "context": {
                            "open_message_id": "om-card",
                            "open_chat_id": "oc-chat",
                        },
                    },
                },
                separators=(",", ":"),
            ).encode()
            timestamp = "1786200000"
            nonce = "nonce"
            signature = hashlib.sha256(
                (timestamp + nonce + "encrypt-key").encode() + callback_body
            ).hexdigest()
            response = await client.post(
                "/feishu/card/callback",
                content=callback_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": signature,
                },
            )
        assert response.status_code == 200
        event = handled[0]
        assert event.message_id == "om-card"
        assert event.action_value["mindflow_action"] == "submit_checkin"

    asyncio.run(verify_url_challenge())


def test_recurrence_builder_exposes_only_reviewed_rfc5545_subset():
    until = datetime(2030, 3, 1, 18, 0, tzinfo=TZ)
    assert build_recurrence_rule(
        "weekly", interval=2, weekdays=["MO", "FR"], until=until
    ) == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR;UNTIL=20300301T100000Z"

    with __import__("pytest").raises(ValueError, match="count and until"):
        build_recurrence_rule("DAILY", count=3, until=until)


def test_generated_weekly_recurrence_requires_start_weekday_in_byday():
    with __import__("pytest").raises(
        ValueError, match="local start weekday"
    ):
        _recurrence_from_args(
            {
                "start_time": "2030-01-07T09:00:00+08:00",  # Monday
                "recurrence_frequency": "WEEKLY",
                "recurrence_weekdays": ["TU"],
            },
            TZ,
        )

    assert _recurrence_from_args(
        {
            "start_time": "2030-01-07T09:00:00+08:00",
            "recurrence_frequency": "WEEKLY",
            "recurrence_weekdays": ["MO", "WE"],
        },
        TZ,
    ) == "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,WE"


def test_feishu_client_sends_card_as_interactive_message():
    seen = []
    client = FeishuClient.__new__(FeishuClient)
    client._send_message = lambda chat_id, msg_type, content: (
        seen.append((chat_id, msg_type, content)) or "om-card"
    )

    card = daily_review_card(
        schedule_id="daily-review-schedule",
        local_date="2030-01-15",
    )
    assert client.send_card("oc-chat", card) == "om-card"
    assert seen == [("oc-chat", "interactive", card)]
    serialized = seen[0][2]
    assert serialized["schema"] == "2.0"
    assert serialized["config"]["update_multi"] is True
    assert any(
        element["tag"] == "form"
        for element in serialized["body"]["elements"]
    )


def test_feishu_client_updates_interactive_card():
    requests = []

    class Messages:
        def patch(self, request):
            requests.append(request)
            return SimpleNamespace(success=lambda: True)

    client = FeishuClient(
        "app",
        "secret",
        sdk_client=SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=Messages()))
        ),
    )
    card = care_intervention_result_card(
        message="提醒", result_text="✓ 已记录：有帮助"
    )
    assert client.update_card("om-card", card) is None
    assert requests[0].message_id == "om-card"
    assert json.loads(requests[0].request_body.content) == card


def test_feishu_client_update_card_surfaces_error():
    class Messages:
        def patch(self, _request):
            return SimpleNamespace(
                success=lambda: False, code=230001, msg="card update rejected"
            )

    client = FeishuClient(
        "app",
        "secret",
        sdk_client=SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=Messages()))
        ),
    )
    with pytest.raises(FeishuSendError, match="card update rejected") as caught:
        client.update_card("om-card", {"schema": "2.0"})
    assert caught.value.operation == "update_card"
    assert caught.value.retryable is False


def test_feishu_client_message_uuid_accepts_50_and_rejects_51_characters():
    requests = []

    class Messages:
        def create(self, request):
            requests.append(request)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(message_id="om-message"),
            )

    client = FeishuClient(
        "app",
        "secret",
        sdk_client=SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=Messages()))
        ),
    )
    assert client.send_text("oc-chat", "ok", message_uuid="a" * 50) == "om-message"
    assert requests[0].request_body.uuid == "a" * 50
    with pytest.raises(ValueError, match="at most 50"):
        client.send_text("oc-chat", "bad", message_uuid="b" * 51)


def test_care_intervention_card_uses_json2_shared_card_contract():
    card = care_intervention_card(
        intervention_id="care-test-id",
        message="测试提醒",
        actions=[
            "helpful",
            "not_relevant",
            "snooze_30",
            "disable_type",
        ],
    )

    assert card["schema"] == "2.0"
    assert card["config"]["update_multi"] is True
    assert card["config"]["enable_forward"] is False

    buttons = []
    for element in card["body"]["elements"]:
        if element.get("tag") != "column_set":
            continue
        assert len(element.get("columns", [])) <= 2
        for column in element.get("columns", []):
            buttons.extend(
                item
                for item in column.get("elements", [])
                if item.get("tag") == "button"
            )

    assert len(buttons) == 4
    payloads = []
    for button in buttons:
        assert "value" not in button
        assert len(button["behaviors"]) == 1
        behavior = button["behaviors"][0]
        assert behavior["type"] == "callback"
        payload = behavior["value"]
        assert payload["version"] == "1"
        assert payload["intervention_id"] == "care-test-id"
        payloads.append(payload["mindflow_action"])

    assert set(payloads) == {
        "care_helpful",
        "care_not_relevant",
        "care_snooze_30",
        "care_disable_type",
    }


def test_care_intervention_result_card_removes_action_buttons():
    card = care_intervention_result_card(
        message="原关怀内容", result_text="✓ 已记录：有帮助"
    )
    serialized = json.dumps(card, ensure_ascii=False)
    assert card["header"]["title"]["content"] == "MindFlow 关怀提醒"
    assert "原关怀内容" in serialized
    assert "✓ 已记录：有帮助" in serialized
    assert '"tag": "button"' not in serialized


def test_care_success_returns_replacement_card():
    database = memory_database()
    person = participant(database, "CARE-RESULT-CARD")
    intervention_id = uuid.uuid4()

    class CareInterventions:
        def apply_action(self, participant_id, requested_id, **_kwargs):
            assert participant_id == person.id
            assert requested_id == intervention_id
            return {
                "created": True,
                "action_result": "recorded",
                "intervention": {"message": "请稍微休息一下。"},
            }

    service = CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(
            on_observation_committed=lambda **_values: None
        ),
        care_interventions=CareInterventions(),
    )
    result = service.handle(
        person.id,
        message_id="om-care",
        callback_event_id="provider-event",
        action_value={
            "mindflow_action": "care_helpful",
            "version": "1",
            "intervention_id": str(intervention_id),
        },
        form_value={},
    )
    serialized = json.dumps(result["card"], ensure_ascii=False)
    assert result["ok"] is True
    assert result["reply_text"]
    assert "请稍微休息一下。" in serialized
    assert "✓ 已记录：有帮助" in serialized
    assert '"tag": "button"' not in serialized


def test_feishu_client_sends_care_card_as_interactive_message():
    seen = []
    client = FeishuClient.__new__(FeishuClient)
    client._send_message = lambda chat_id, msg_type, content: (
        seen.append((chat_id, msg_type, content)) or "om-care-card"
    )

    card = care_intervention_card(
        intervention_id="care-test-id",
        message="测试提醒",
        actions=["helpful", "not_relevant"],
    )
    message_id = client.send_card("oc-test", card)

    assert message_id == "om-care-card"
    assert seen == [("oc-test", "interactive", card)]
    assert seen[0][2]["config"]["update_multi"] is True


def test_json2_callback_buttons_never_use_root_value():
    def buttons_in(value):
        found = []
        if isinstance(value, dict):
            if value.get("tag") == "button":
                found.append(value)
            for nested in value.values():
                found.extend(buttons_in(nested))
        elif isinstance(value, list):
            for nested in value:
                found.extend(buttons_in(nested))
        return found

    analysis = analyze_curve([
        {"time": "09:00", "stress_0_10": 4.0, "vitality_0_10": 7.0},
        {"time": "10:00", "stress_0_10": 8.0, "vitality_0_10": 5.0},
    ])
    cards = [
        care_intervention_card(
            intervention_id="care-test-id",
            message="测试提醒",
            actions=["helpful", "not_relevant"],
        ),
        pressure_curve_card(
            analysis,
            image_key="img-key",
            local_date="2030-01-15",
        ),
        today_calendar_card([], local_date="2030-01-15"),
        daily_checkin_card(),
        daily_review_card(
            schedule_id="daily-review-schedule",
            local_date="2030-01-15",
        ),
    ]

    for card in cards:
        assert card["schema"] == "2.0"
        buttons = buttons_in(card)
        assert buttons
        for button in buttons:
            assert "value" not in button
            assert len(button.get("behaviors") or []) == 1
            assert button["behaviors"][0]["type"] == "callback"
            assert button["behaviors"][0]["value"]["mindflow_action"]


def test_pressure_curve_tool_stages_reviewed_card_for_current_run():
    class Coordinator:
        async def ensure_forecast(self, participant_id, *_args, **_kwargs):
            assert participant_id == context.participant_id
            return {
                "local_date": "2030-01-15",
                "curve": [
                    {"time": "09:00", "stress_0_10": 3.0, "vitality_0_10": 8.0},
                    {"time": "10:00", "stress_0_10": 7.5, "vitality_0_10": 5.0},
                ],
                "calendar_degraded": False,
            }

    context = AgentContext(
        uuid.uuid4(), "P001", "ou", "oc", "message", uuid.uuid4()
    )
    outbox = PresentationOutbox()
    tools = CareTools(
        None, None, None, None, "Asia/Shanghai",
        Coordinator(), None, outbox,
    )

    result = asyncio.run(tools.get_pressure_curve(context, {}))

    assert result["card_queued"] is True
    assert result["predicted_peak"] == {"time": "10:00", "stress_0_10": 7.5}
    cards = outbox.take_cards(context.agent_run_id)
    assert len(cards) == 1
    assert isinstance(cards[0], PendingImageCard)
    assert cards[0].png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    materialized = cards[0].materialize("img-key")
    assert materialized["body"]["elements"][0]["tag"] == "img"
    assert materialized["body"]["elements"][0]["img_key"] == "img-key"
    assert materialized["header"]["title"]["content"] == "今日压力趋势"
    visible_card_text = str(materialized)
    assert "M0" not in visible_card_text
    assert "stress-ctssm" not in visible_card_text
    assert "关键时段" in visible_card_text
    assert "仅供日常状态参考" in visible_card_text


def test_worker_delivers_staged_card_before_final_text():
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=4)
    gateway = FeishuGateway("app", "secret", identity, events, queue)
    outbox = PresentationOutbox()

    class Runtime:
        async def handle_message(self, ctx, _text, **_kwargs):
            outbox.stage_card(ctx.agent_run_id, {"schema": "2.0", "body": {}})
            return "曲线卡片已生成。"

    class Sender:
        def __init__(self):
            self.sent = []

        def send_text(self, chat_id, text):
            self.sent.append(("text", chat_id, text))
            return f"om-{len(self.sent)}"

        def send_card(self, chat_id, card):
            self.sent.append(("card", chat_id, card))
            return f"om-{len(self.sent)}"

    sender = Sender()
    worker = BotWorker(
        queue, identity, events, AgentRunRepository(database),
        SkillLoader(skill_path()), Runtime(), sender, None, outbox,
        model="fake",
        generic_progress_delay_seconds=60,
        tool_progress_grace_seconds=60,
    )

    async def scenario():
        gateway.accept_payload({
            "header": {"event_id": "bind-event"},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou"}},
                "message": {
                    "message_id": "bind-message", "chat_id": "oc", "chat_type": "p2p",
                    "message_type": "text",
                    "content": __import__("json").dumps({"text": f"/bind {code}"}),
                },
            },
        })
        await worker.process(await queue.get())
        gateway.accept_event(BotEvent(
            "curve-event", "curve-message", "app", "ou", "oc", "给我压力曲线",
            datetime.now(TZ), "p2p",
        ))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert [item[0] for item in sender.sent[-2:]] == ["card", "text"]
    assert sender.sent[-1][2] == "曲线卡片已生成。"


def test_worker_logs_feishu_card_failure_details_and_sends_fallback(caplog):
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=4)
    gateway = FeishuGateway("app", "secret", identity, events, queue)
    outbox = PresentationOutbox()
    sensitive_card_value = "access-token-must-not-be-logged"

    class Runtime:
        async def handle_message(self, ctx, _text, **_kwargs):
            outbox.stage_card(ctx.agent_run_id, {
                "schema": "2.0",
                "body": {"elements": [{"content": sensitive_card_value}]},
            })
            return "曲线卡片已生成。"

    class Sender:
        def __init__(self):
            self.sent = []

        def send_text(self, chat_id, text):
            self.sent.append(("text", chat_id, text))
            return f"om-{len(self.sent)}"

        def send_card(self, chat_id, card):
            self.sent.append(("card", chat_id, card))
            raise FeishuSendError(
                "Failed to create card content, ext=ErrPath config.update_multi",
                code=230099,
                retryable=True,
                operation="send_message",
            )

    sender = Sender()
    worker = BotWorker(
        queue, identity, events, AgentRunRepository(database),
        SkillLoader(skill_path()), Runtime(), sender, None, outbox,
        model="fake",
        max_retries=0,
        generic_progress_delay_seconds=60,
        tool_progress_grace_seconds=60,
    )

    async def scenario():
        gateway.accept_payload({
            "header": {"event_id": "bind-event"},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou"}},
                "message": {
                    "message_id": "bind-message", "chat_id": "oc", "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": f"/bind {code}"}),
                },
            },
        })
        await worker.process(await queue.get())
        gateway.accept_event(BotEvent(
            "curve-event", "curve-message", "app", "ou", "oc", "给我压力曲线",
            datetime.now(TZ), "p2p",
        ))
        await worker.process(await queue.get())

    caplog.set_level("WARNING")
    asyncio.run(scenario())

    assert [item[0] for item in sender.sent[-2:]] == ["card", "text"]
    assert sender.sent[-1][2] == (
        "曲线卡片已生成。\n\n卡片暂时未能发送，请稍后再试。"
    )
    assert "230099" in caplog.text
    assert "Failed to create card content" in caplog.text
    assert "ErrPath config.update_multi" in caplog.text
    assert "operation=send_message" in caplog.text
    assert sensitive_card_value not in caplog.text


def test_calendar_create_event_uses_user_token_primary_calendar_and_idempotency(monkeypatch):
    person_id = uuid.uuid4()

    class Tokens:
        async def get_access_token(self, participant_id):
            assert participant_id == person_id
            return "user-token"

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        calls = []

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, params, json=None):
            self.calls.append((url, headers, params, json))
            if url.endswith("/calendars/primary"):
                return Response({
                    "code": 0,
                    "data": {"calendar": {"calendar_id": "primary/calendar"}},
                })
            return Response({
                "code": 0,
                "data": {
                    "event": {
                        "event_id": "event-1",
                        "summary": json["summary"],
                        "description": json["description"],
                        "start_time": json["start_time"],
                        "end_time": json["end_time"],
                    }
                },
            })

    monkeypatch.setattr("app.integrations.feishu.calendar.httpx.AsyncClient", Client)
    calendar = CalendarService(Tokens(), timezone_name="Asia/Shanghai")
    start = datetime(2030, 1, 15, 9, 0, tzinfo=TZ)
    result = asyncio.run(calendar.create_event(
        person_id,
        summary="项目复盘",
        description="讨论下一步",
        start_time=start,
        end_time=start + timedelta(hours=1),
        reminder_minutes=15,
        source_message_id="message-1",
    ))

    assert result["id"] == "event-1"
    assert Client.calls[0][1] == {"Authorization": "Bearer user-token"}
    create_call = Client.calls[1]
    assert create_call[0].endswith("/calendars/primary%2Fcalendar/events")
    assert len(create_call[2]["idempotency_key"]) == 64
    assert create_call[3]["reminders"] == [{"minutes": 15}]
    assert create_call[3]["start_time"]["timezone"] == "Asia/Shanghai"


def test_calendar_update_and_delete_use_exact_primary_calendar_event(monkeypatch):
    person_id = uuid.uuid4()

    class Tokens:
        async def get_access_token(self, participant_id):
            assert participant_id == person_id
            return "user-token"

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        calls = []

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            return Response({"code": 0, "data": {"calendar": {"calendar_id": "primary/calendar"}}})

        async def patch(self, url, **kwargs):
            self.calls.append(("PATCH", url, kwargs))
            return Response({
                "code": 0,
                "data": {"event": {"event_id": "event/1", **kwargs["json"]}},
            })

        async def delete(self, url, **kwargs):
            self.calls.append(("DELETE", url, kwargs))
            return Response({"code": 0, "data": {}})

    monkeypatch.setattr("app.integrations.feishu.calendar.httpx.AsyncClient", Client)
    calendar = CalendarService(Tokens())
    updated = asyncio.run(calendar.update_event(
        person_id,
        "event/1",
        summary="新标题",
        recurrence="FREQ=WEEKLY;INTERVAL=1;BYDAY=MO",
    ))
    deleted = asyncio.run(calendar.delete_event(person_id, "event/1"))

    patch_call = next(call for call in Client.calls if call[0] == "PATCH")
    delete_call = next(call for call in Client.calls if call[0] == "DELETE")
    assert patch_call[1].endswith("/events/event%2F1")
    assert patch_call[2]["json"]["recurrence"].startswith("FREQ=WEEKLY")
    assert updated["summary"] == "新标题"
    assert delete_call[1].endswith("/events/event%2F1")
    assert deleted == {"id": "event/1", "deleted": True}


def test_card_action_adapter_and_service_record_one_idempotent_checkin():
    database = memory_database()
    person = participant(database, "P001")
    observations = ObservationRepository(database)
    service = CardActionService(
        observations,
        observation_refresh=SimpleNamespace(
            on_observation_committed=lambda **_values: None
        ),
    )
    raw = SimpleNamespace(
        message_id="om-card",
        chat_id="oc-chat",
        operator=SimpleNamespace(open_id="ou-user"),
        action=SimpleNamespace(
            tag="button",
            value={"mindflow_action": "submit_checkin", "version": "1"},
            form_value={
                "stress": "7",
                "energy": "4",
                "activity": "写课程作业",
                "stress_event_since_last": "true",
                "event_ongoing": "false",
            },
        ),
    )
    event = FeishuCardActionAdapter("app").adapt(raw)

    p2_event = FeishuCardActionAdapter("app").adapt_p2(
        SimpleNamespace(
            event=SimpleNamespace(
                context=SimpleNamespace(
                    open_message_id="om-card", open_chat_id="oc-chat"
                ),
                operator=SimpleNamespace(open_id="ou-user"),
                action=raw.action,
            )
        )
    )

    first = service.handle(
        person.id,
        message_id=event.message_id,
        action_value=event.action_value,
        form_value=event.form_value,
    )
    second = service.handle(
        person.id,
        message_id=event.message_id,
        action_value=event.action_value,
        form_value=event.form_value,
    )

    assert event.event_id.startswith("card:")
    assert p2_event == event
    assert first["observation_id"] == second["observation_id"]
    recorded = observations.recent(person.id, limit=5)
    assert len(recorded) == 1
    assert recorded[0]["payload"]["input_method"] == "feishu_card"
    assert recorded[0]["payload"]["stress_0_10"] == 7.0
