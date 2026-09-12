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
