"""Safety boundary: fixed response only, no researcher path, no durable labels.

The reviewed product decision: a Safety hit never notifies researchers, never
enters participant-visible admin views as a risk label, never writes a
long-term profile or memory record, and never schedules a mandatory follow-up
message. Only anonymous aggregate operational metrics are allowed.
"""

from pathlib import Path

from app.agent.claude_runtime import ClaudeAgentRuntime
from app.agent.context import AgentContext
from app.services.safety_service import SafetyService
from helpers import memory_database, participant
import asyncio
import uuid


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
SAFETY_SOURCE = RUNTIME_ROOT / "app" / "services" / "safety_service.py"
RUNTIME_SOURCE = RUNTIME_ROOT / "app" / "agent" / "claude_runtime.py"


def test_safety_modules_have_no_researcher_notification_path():
    for path in (SAFETY_SOURCE, RUNTIME_SOURCE):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "RuntimeIncident",
            "incidents",
            "send_text",
            "send_card",
            "FeishuClient",
            "admin_web",
            "researcher",
        ):
            assert forbidden not in source, (
                f"{path.name} must not reference {forbidden}; a Safety hit "
                "never reaches a researcher notification path"
            )


def test_safety_modules_write_no_profile_or_long_term_memory():
    for path in (SAFETY_SOURCE, RUNTIME_SOURCE):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "ProfileRepository",
            "LearnedProfileRepository",
            "ParticipantProfile",
            "ParticipantMemory",
            "WarningScheduleRepository",
        ):
            assert forbidden not in source, (
                f"{path.name} must not reference {forbidden}; Safety results "
                "never enter a long-term profile or memory"
            )


def test_safety_boundary_does_not_schedule_mandatory_follow_up():
    source = SAFETY_SOURCE.read_text(encoding="utf-8")
    for forbidden in (
        "Scheduler",
        "care_interventions",
        "CareIntervention",
        "next_day",
        "follow_up_message",
    ):
        assert forbidden not in source, (
            f"safety_service.py must not reference {forbidden}; supportive "
            "follow-up belongs to the care policy, not safety escalation"
        )


def test_safety_locked_turn_leaves_only_conversation_rows_behind():
    database = memory_database()
    person = participant(database, "P-SAFETY-BOUNDARY")

    class NoSessions:
        async def submit(self, *_args, **_kwargs):
            raise AssertionError("SDK must not receive safety-locked text")

    runtime = ClaudeAgentRuntime(
        NoSessions(),
        __import__("app.repositories", fromlist=["ConversationRepository"]).ConversationRepository(database),
        SafetyService(),
    )
    ctx = AgentContext(
        person.id, "P-SAFETY-BOUNDARY", "ou", "oc", "msg", uuid.uuid4()
    )
    result = asyncio.run(
        runtime.handle_message(ctx, "我不想活了", chat_type="p2p")
    )
    assert result.safety_locked is True
    assert result.response_kind == "fixed"

    from sqlalchemy import select

    from app.models import ParticipantProfile

    with database.session() as session:
        profiles = list(session.execute(select(ParticipantProfile)).scalars())
    assert profiles == []


def test_safety_hit_marks_bot_event_protected_and_admin_view_redacts():
    from datetime import datetime, timezone

    from app.admin_web.repositories import AdminRepository
    from app.repositories import BotEventRepository

    database = memory_database()
    person = participant(database, "P-SAFETY-REDACT")
    events = BotEventRepository(database)
    events.accept(
        "evt-risk",
        "om-risk",
        person.id,
        app_id="cli",
        open_id="ou",
        chat_id="oc",
        chat_type="p2p",
        message_type="text",
        text="我不想活了",
        create_time=datetime.now(timezone.utc),
    )
    events.accept(
        "evt-normal",
        "om-normal",
        person.id,
        app_id="cli",
        open_id="ou",
        chat_id="oc",
        chat_type="p2p",
        message_type="text",
        text="帮我看看今天的安排",
        create_time=datetime.now(timezone.utc),
    )

    events.mark_content_protected("evt-risk")

    admin = AdminRepository(database)
    views = {item["event_id"]: item for item in admin.messages(person.id)}
    protected = views["evt-risk"]
    assert protected["content_redacted"] is True
    assert protected["content_privacy_class"] == "protected"
    assert "我不想活了" not in protected["text"]
    assert protected["text"] == "[内容受隐私保护]"
    assert protected["reply_text"] == "[内容受隐私保护]"
    normal = views["evt-normal"]
    assert normal["content_redacted"] is False
    assert normal["text"] == "帮我看看今天的安排"

    detail = admin.message("evt-risk")
    assert detail["content_redacted"] is True
    assert "我不想活了" not in str(detail)


def test_worker_safety_locked_turn_persists_protected_privacy_class():
    import asyncio as _asyncio
    import json as _json

    from app.presentation.contracts import RuntimeResponse
    from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
    from app.agent.skill_loader import SkillLoader
    from app.identity.service import IdentityService
    from app.integrations.feishu.gateway import FeishuGateway
    from app.worker import BotWorker
    from helpers import skill_path
    from sqlalchemy import select

    from app.models import BotEvent

    database = memory_database()
    person = participant(database, "P-SAFETY-WORKER")
    identity = IdentityService(database, BindingRepository(database))
    events = BotEventRepository(database)
    queue = _asyncio.Queue(maxsize=8)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    sender_messages = []

    class Sender:
        def send_text(self, chat_id, text):
            sender_messages.append((chat_id, text))
            return f"out-{len(sender_messages)}"

    class SafetyRuntime:
        async def handle_message(self, ctx, turn_input, **_kwargs):
            return RuntimeResponse(
                text="你已经很重要。此刻如有紧急情况，请立即拨打当地紧急电话或前往最近医院急诊。",
                safety_locked=True,
                response_kind="fixed",
            )

    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        SafetyRuntime(),
        Sender(),
        model="fake",
        consent_service=__import__(
            "app.services.consent_service", fromlist=["ConsentService"]
        ).ConsentService(
            __import__(
                "app.repositories_consent", fromlist=["ParticipantConsentRepository"]
            ).ParticipantConsentRepository(database)
        ),
    )

    def payload(event_id, message_id, text):
        return {
            "header": {"event_id": event_id},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou-sw"}},
                "message": {
                    "message_id": message_id,
                    "chat_id": "oc-sw",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": _json.dumps({"text": text}),
                },
            },
        }

    code, _ = identity.create_invite(person.id)

    async def scenario():
        assert gateway.accept_payload(
            payload("sw-bind", "m-bind", f"/bind {code}")
        )
        await worker.process(await queue.get())
        assert gateway.accept_payload(
            payload("sw-risk", "m-sw", "我不想活了")
        )
        await worker.process(await queue.get())

    _asyncio.run(scenario())
    with database.session() as session:
        row = session.execute(
            select(BotEvent).where(BotEvent.event_id == "sw-risk")
        ).scalar_one()
    assert row.content_privacy_class == "protected"
    from app.admin_web.repositories import AdminRepository

    view = AdminRepository(database).message("sw-risk")
    assert view["content_redacted"] is True
    assert "我不想活了" not in view["text"]


def test_bot_event_schema_rejects_risk_label_privacy_classes():
    """Only normal/protected exist; classifier labels can never be stored."""

    from datetime import datetime, timezone

    from sqlalchemy.exc import IntegrityError

    from app.models import BotEvent

    database = memory_database()
    with database.session() as session:
        session.add(
            BotEvent(
                event_id="evt-label",
                message_id="om-label",
                app_id="cli",
                open_id="ou",
                chat_id="oc",
                chat_type="p2p",
                message_type="text",
                text="text",
                message_created_at=datetime.now(timezone.utc),
                status="completed",
                content_privacy_class="high_risk",
            )
        )
        raised = None
        try:
            session.flush()
        except IntegrityError as exc:
            raised = exc
            session.rollback()
    assert raised is not None
    assert "ck_bot_event_content_privacy_class" in str(raised)
