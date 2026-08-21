import asyncio
from datetime import datetime, timedelta
import uuid
from zoneinfo import ZoneInfo

from app.agent.context import AgentContext
from app.agent.skill_loader import SkillLoader
from app.integrations.feishu.calendar import CalendarService
from app.integrations.feishu.cards import pressure_curve_card
from app.integrations.feishu.client import FeishuClient
from app.integrations.feishu.gateway import BotEvent, FeishuGateway
from app.identity.service import IdentityService
from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
from app.services.presentation_service import PresentationOutbox
from app.tools.care import CareTools
from app.worker import BotWorker
from helpers import memory_database, participant, skill_path


TZ = ZoneInfo("Asia/Shanghai")


def test_pressure_curve_card_contains_native_feishu_line_chart():
    card = pressure_curve_card(
        [
            {"time": "09:00", "stress_0_10": 4.5, "vitality_0_10": 7.0},
            {"time": "10:00", "stress_0_10": 8.0, "vitality_0_10": 5.0},
        ],
        local_date="2030-01-15",
    )

    assert card["schema"] == "2.0"
    chart = card["body"]["elements"][0]
    assert chart["tag"] == "chart"
    assert chart["chart_spec"]["type"] == "line"
    assert chart["chart_spec"]["yField"] == "value"
    assert {item["metric"] for item in chart["chart_spec"]["data"][0]["values"]} == {
        "压力",
        "活力",
    }


def test_feishu_client_sends_card_as_interactive_message():
    seen = []
    client = FeishuClient.__new__(FeishuClient)
    client._send_message = lambda chat_id, msg_type, content: (
        seen.append((chat_id, msg_type, content)) or "om-card"
    )

    card = {"schema": "2.0", "body": {"elements": []}}
    assert client.send_card("oc-chat", card) == "om-card"
    assert seen == [("oc-chat", "interactive", card)]


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
        None, None, None, None, None, None, "Asia/Shanghai",
        Coordinator(), None, outbox,
    )

    result = asyncio.run(tools.get_pressure_curve(context, {}))

    assert result["card_queued"] is True
    assert result["predicted_peak"] == {"time": "10:00", "stress_0_10": 7.5}
    cards = outbox.take_cards(context.agent_run_id)
    assert len(cards) == 1
    assert cards[0]["body"]["elements"][0]["tag"] == "chart"


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
        model="fake", progress_delay_seconds=60,
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
