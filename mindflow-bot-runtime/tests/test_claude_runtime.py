import asyncio
import uuid

import pytest

from app.agent.claude_runtime import ClaudeAgentRuntime
from app.agent.context import AgentContext
from app.agent.sdk_adapter import ClaudeSDKTurnInterrupted, ClaudeTurnResult
from app.agent.session_manager import ParticipantSessionManager
from app.repositories import ClaudeSessionRepository, ConversationRepository
from app.services.safety_service import FIXED_HIGH_RISK_RESPONSE, SafetyService
from helpers import memory_database, participant


def context(participant_id, marker):
    return AgentContext(
        participant_id,
        f"P{marker}",
        f"ou_{marker}",
        f"oc_{marker}",
        f"msg_{marker}",
        uuid.uuid4(),
    )


class FakeClient:
    def __init__(self, binding, resume, factory):
        self.binding = binding
        self.resume = resume
        self.factory = factory
        self.connected = False
        self.interrupted = False
        self.release = asyncio.Event()
        self.release.set()

    async def connect(self):
        self.connected = True

    async def run_turn(self, text, on_tool_use=None):
        ctx = self.binding.require()
        self.factory.active += 1
        self.factory.max_active = max(self.factory.max_active, self.factory.active)
        self.factory.turns.append((ctx.participant_id, text))
        try:
            await self.release.wait()
            if self.interrupted:
                raise ClaudeSDKTurnInterrupted("interrupted")
            if on_tool_use:
                await on_tool_use("care_get_today_context")
            session_id = self.resume or f"session-{ctx.participant_id}"
            return ClaudeTurnResult(f"answer:{text}", session_id)
        finally:
            self.factory.active -= 1

    async def interrupt(self):
        self.interrupted = True
        self.release.set()

    async def disconnect(self):
        self.connected = False


class FakeFactory:
    def __init__(self):
        self.created = []
        self.turns = []
        self.active = 0
        self.max_active = 0
        self.block_next = False

    def validate(self):
        return None

    def create(self, binding, *, resume_session_id):
        client = FakeClient(binding, resume_session_id, self)
        if self.block_next:
            client.release.clear()
            self.block_next = False
        self.created.append(client)
        return client


def test_sessions_are_persistent_serial_and_participant_isolated():
    database = memory_database()
    p1 = participant(database, "P001")
    p2 = participant(database, "P002")
    repository = ClaudeSessionRepository(database)

    async def scenario():
        first_factory = FakeFactory()
        manager = ParticipantSessionManager(
            first_factory, repository, max_active_sessions=2, idle_timeout_seconds=60
        )
        first, second = await asyncio.gather(
            manager.submit(context(p1.id, "001a"), "first"),
            manager.submit(context(p1.id, "001b"), "second"),
        )
        other = await manager.submit(context(p2.id, "002"), "other")
        assert first.session_id == second.session_id
        assert other.session_id != first.session_id
        assert [text for pid, text in first_factory.turns if pid == p1.id] == [
            "first",
            "second",
        ]
        assert first_factory.max_active <= 2
        await manager.close()

        resumed_factory = FakeFactory()
        resumed = ParticipantSessionManager(resumed_factory, repository)
        await resumed.submit(context(p1.id, "001c"), "third")
        assert resumed_factory.created[0].resume == first.session_id
        await resumed.close()

    asyncio.run(scenario())
    assert repository.get(p1.id).last_message_id == "msg_001c"
    assert repository.get(p2.id).session_id.endswith(str(p2.id))


def test_explicit_interrupt_reaches_running_client():
    database = memory_database()
    p1 = participant(database, "P001")
    factory = FakeFactory()
    factory.block_next = True
    manager = ParticipantSessionManager(factory, ClaudeSessionRepository(database))

    async def scenario():
        turn = asyncio.create_task(manager.submit(context(p1.id, "stop"), "long"))
        for _ in range(100):
            if factory.created and factory.created[0].binding.current is not None:
                break
            await asyncio.sleep(0.001)
        assert await manager.interrupt(p1.id) is True
        with pytest.raises(ClaudeSDKTurnInterrupted):
            await turn
        await manager.close()

    asyncio.run(scenario())


def test_safety_precheck_never_submits_high_risk_text_to_sdk():
    database = memory_database()
    p1 = participant(database, "P001")

    class NoSessions:
        async def submit(self, *_args, **_kwargs):
            raise AssertionError("SDK must not receive prechecked high-risk text")

    runtime = ClaudeAgentRuntime(
        NoSessions(), ConversationRepository(database), SafetyService()
    )
    result = asyncio.run(
        runtime.handle_message(context(p1.id, "risk"), "我想自杀")
    )
    assert result == FIXED_HIGH_RISK_RESPONSE
    saved = ConversationRepository(database).recent(p1.id, 10)
    assert [item["role"] for item in saved] == ["user", "assistant"]
