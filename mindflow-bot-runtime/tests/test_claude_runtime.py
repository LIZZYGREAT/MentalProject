import asyncio
from datetime import datetime, timezone
import uuid

import pytest

from app.agent.claude_runtime import ClaudeAgentRuntime
from app.agent.context import AgentContext
from app.agent.sdk_adapter import (
    ClaudeSDKInvocationError,
    ClaudeSDKTurnInterrupted,
    ClaudeTurnResult,
)
from app.agent.session_manager import ParticipantSessionManager
from app.contracts.agent_input import AgentTurnInput
from app.presentation.contracts import AgentActivityEvent
from app.repositories import (
    BotEventRepository,
    ClaudeSessionRepository,
    ConversationRepository,
)
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

    async def run_turn(self, turn_input):
        ctx = self.binding.require()
        self.factory.active += 1
        self.factory.max_active = max(self.factory.max_active, self.factory.active)
        self.factory.turns.append((ctx.participant_id, turn_input.text))
        try:
            await self.release.wait()
            if self.interrupted:
                raise ClaudeSDKTurnInterrupted("interrupted")
            await self.binding.emit(
                AgentActivityEvent(
                    kind="tool_started", tool_name="care_get_today_context"
                )
            )
            await self.binding.emit(
                AgentActivityEvent(
                    kind="tool_succeeded",
                    tool_name="care_get_today_context",
                    status="succeeded",
                )
            )
            session_id = self.resume or f"session-{ctx.participant_id}"
            return ClaudeTurnResult(f"answer:{turn_input.text}", session_id)
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


class FailingInterruptClient(FakeClient):
    async def interrupt(self):
        raise RuntimeError("interrupt transport failed")


class FailingInterruptFactory(FakeFactory):
    def create(self, binding, *, resume_session_id):
        client = FailingInterruptClient(binding, resume_session_id, self)
        client.release.clear()
        self.created.append(client)
        return client


class DelayedFailingInterruptClient(FakeClient):
    async def interrupt(self):
        self.factory.interrupt_started.set()
        await self.factory.release_interrupt.wait()
        raise RuntimeError("interrupt transport failed")


class RecoveringAfterInterruptFailureFactory(FakeFactory):
    def __init__(self):
        super().__init__()
        self.interrupt_started = asyncio.Event()
        self.release_interrupt = asyncio.Event()

    def create(self, binding, *, resume_session_id):
        if not self.created:
            client = DelayedFailingInterruptClient(
                binding, resume_session_id, self
            )
            client.release.clear()
        else:
            client = FakeClient(binding, resume_session_id, self)
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
        assert all(
            client.binding.activity_callback is None
            for client in first_factory.created
        )
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


def test_stop_interrupt_failure_does_not_leave_pending_queue():
    database = memory_database()
    p1 = participant(database, "P001")
    factory = FailingInterruptFactory()
    manager = ParticipantSessionManager(factory, ClaudeSessionRepository(database))

    async def scenario():
        first = asyncio.create_task(manager.submit(context(p1.id, "first"), "first"))
        for _ in range(100):
            if factory.turns:
                break
            await asyncio.sleep(0.001)
        second = asyncio.create_task(
            manager.submit(context(p1.id, "second"), "second")
        )
        await asyncio.sleep(0)

        with pytest.raises(ClaudeSDKInvocationError, match="interrupt failed"):
            await manager.interrupt(p1.id)

        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert factory.turns == [(p1.id, "first")]
        session = manager._sessions.get(p1.id)
        assert session is None or session.queue.empty()
        await manager.close()

    asyncio.run(scenario())


def test_request_arriving_during_interrupt_failure_cannot_be_stranded():
    database = memory_database()
    p1 = participant(database, "P001")
    factory = RecoveringAfterInterruptFailureFactory()
    manager = ParticipantSessionManager(factory, ClaudeSessionRepository(database))

    async def scenario():
        first = asyncio.create_task(manager.submit(context(p1.id, "first"), "first"))
        for _ in range(100):
            if factory.turns:
                break
            await asyncio.sleep(0.001)
        interrupt = asyncio.create_task(manager.interrupt(p1.id))
        await factory.interrupt_started.wait()
        raced = asyncio.create_task(
            manager.submit(context(p1.id, "raced"), "raced")
        )
        await asyncio.sleep(0)
        factory.release_interrupt.set()

        with pytest.raises(ClaudeSDKInvocationError, match="interrupt failed"):
            await interrupt
        stopped = await asyncio.gather(first, raced, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in stopped)

        recovered = await manager.submit(context(p1.id, "recovered"), "recovered")
        assert recovered.text == "answer:recovered"
        await manager.close()

    asyncio.run(scenario())
    assert [text for _participant_id, text in factory.turns] == [
        "first",
        "recovered",
    ]


def test_stop_skips_cancelled_queued_agent_request():
    database = memory_database()
    p1 = participant(database, "P001")
    factory = FakeFactory()
    factory.block_next = True
    manager = ParticipantSessionManager(factory, ClaudeSessionRepository(database))

    async def scenario():
        first = asyncio.create_task(manager.submit(context(p1.id, "first"), "first"))
        for _ in range(100):
            if factory.turns:
                break
            await asyncio.sleep(0.001)
        second = asyncio.create_task(
            manager.submit(context(p1.id, "second"), "second")
        )
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        factory.created[0].release.set()
        await first
        await manager.close()

    asyncio.run(scenario())
    assert [text for _participant_id, text in factory.turns] == ["first"]


def test_stop_after_two_images_when_second_is_agent_queued_runs_no_second_sdk_turn():
    database = memory_database()
    p1 = participant(database, "P001")
    factory = FakeFactory()
    factory.block_next = True
    manager = ParticipantSessionManager(factory, ClaudeSessionRepository(database))

    async def scenario():
        first = asyncio.create_task(
            manager.submit(
                context(p1.id, "image-1"),
                AgentTurnInput(text="", trusted_image_context={"image": "one"}),
            )
        )
        for _ in range(100):
            if factory.turns:
                break
            await asyncio.sleep(0.001)
        second = asyncio.create_task(
            manager.submit(
                context(p1.id, "image-2"),
                AgentTurnInput(text="", trusted_image_context={"image": "two"}),
            )
        )
        await asyncio.sleep(0)
        assert await manager.interrupt(p1.id) is True
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], ClaudeSDKTurnInterrupted)
        assert isinstance(results[1], asyncio.CancelledError)
        await manager.close()

    asyncio.run(scenario())
    assert len(factory.turns) == 1


def test_stop_prevents_queued_image_calendar_mutation():
    database = memory_database()
    p1 = participant(database, "P001")
    factory = FakeFactory()
    factory.block_next = True
    manager = ParticipantSessionManager(factory, ClaudeSessionRepository(database))
    queued_context = context(p1.id, "calendar-image")
    queued_context = AgentContext(
        queued_context.participant_id,
        queued_context.participant_code,
        queued_context.open_id,
        queued_context.chat_id,
        queued_context.message_id,
        queued_context.agent_run_id,
        calendar_mutation_policy="calendar_create_only",
    )

    async def scenario():
        first = asyncio.create_task(manager.submit(context(p1.id, "active"), "active"))
        for _ in range(100):
            if factory.turns:
                break
            await asyncio.sleep(0.001)
        queued = asyncio.create_task(
            manager.submit(
                queued_context,
                AgentTurnInput(
                    text="把这个讲座添加到日历",
                    trusted_image_context={"image_kind": "poster"},
                ),
            )
        )
        await asyncio.sleep(0)
        assert await manager.interrupt(p1.id) is True
        await asyncio.gather(first, queued, return_exceptions=True)
        await manager.close()

    asyncio.run(scenario())
    assert [text for _participant_id, text in factory.turns] == ["active"]


def test_activity_callbacks_are_isolated_by_participant_and_cleared_after_turn():
    database = memory_database()
    p1 = participant(database, "P001")
    p2 = participant(database, "P002")
    factory = FakeFactory()
    manager = ParticipantSessionManager(
        factory,
        ClaudeSessionRepository(database),
        max_active_sessions=2,
    )
    received = {p1.id: [], p2.id: []}

    async def scenario():
        async def callback_for(participant_id, event):
            received[participant_id].append((event.kind, event.tool_name))

        await asyncio.gather(
            manager.submit(
                context(p1.id, "activity-1"),
                "one",
                on_activity=lambda event: callback_for(p1.id, event),
            ),
            manager.submit(
                context(p2.id, "activity-2"),
                "two",
                on_activity=lambda event: callback_for(p2.id, event),
            ),
        )
        assert all(
            client.binding.activity_callback is None for client in factory.created
        )
        await manager.close()

    asyncio.run(scenario())
    expected = [
        ("tool_started", "care_get_today_context"),
        ("tool_succeeded", "care_get_today_context"),
    ]
    assert received[p1.id] == expected
    assert received[p2.id] == expected


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
    assert result.safety_locked is True
    assert result.response_kind == "fixed"
    saved = ConversationRepository(database).recent(p1.id, 10)
    assert [item["role"] for item in saved] == ["user", "assistant"]


def test_multimodal_safety_precheck_handles_image_only_self_harm_content():
    database = memory_database()
    p1 = participant(database, "P-IMAGE-RISK")

    class NoSessions:
        async def submit(self, *_args, **_kwargs):
            raise AssertionError("high-risk image context must not reach the SDK")

    runtime = ClaudeAgentRuntime(
        NoSessions(), ConversationRepository(database), SafetyService()
    )
    result = asyncio.run(runtime.handle_message(
        context(p1.id, "image-risk"),
        AgentTurnInput(
            text="",
            trusted_image_context={
                "image_kind": "document",
                "summary": "一张写着我不想活了的求助便签",
                "visible_text": "我想结束生命",
                "warnings": [],
            },
        ),
    ))
    assert result.text == FIXED_HIGH_RISK_RESPONSE
    assert result.safety_locked is True


def test_multimodal_safety_precheck_allows_third_party_news_summary():
    database = memory_database()
    p1 = participant(database, "P-IMAGE-NEWS")

    class Sessions:
        def __init__(self):
            self.turns = []

        async def submit(self, _ctx, turn_input, **_kwargs):
            self.turns.append(turn_input)
            return ClaudeTurnResult("这是对新闻内容的中性总结。", "session-news")

    sessions = Sessions()
    runtime = ClaudeAgentRuntime(
        sessions, ConversationRepository(database), SafetyService()
    )
    result = asyncio.run(runtime.handle_message(
        context(p1.id, "image-news"),
        AgentTurnInput(
            text="总结这篇新闻",
            trusted_image_context={
                "image_kind": "document",
                "summary": "一篇关于自杀预防研究的新闻报道",
                "visible_text": "研究人员讨论自杀预防",
                "warnings": [],
            },
        ),
    ))
    assert result.text == "这是对新闻内容的中性总结。"
    assert result.safety_locked is False
    assert len(sessions.turns) == 1


def test_runtime_supplies_backend_owned_recent_turns_to_agent_context():
    class Conversations:
        def __init__(self):
            self.recent_calls = []

        def add(self, *_args, **_kwargs):
            return uuid.uuid4()

        def recent(self, participant_id, limit, **kwargs):
            self.recent_calls.append((participant_id, limit, kwargs))
            return [
                {"role": "user", "content": "明天下午项目组会几点？"},
                {"role": "assistant", "content": "15:00到16:00。"},
            ]

    class Sessions:
        def __init__(self):
            self.contexts = []

        async def submit(self, ctx, _turn_input, **_kwargs):
            self.contexts.append(ctx)
            return ClaudeTurnResult("好的", "session-context")

    conversations = Conversations()
    sessions = Sessions()
    runtime = ClaudeAgentRuntime(sessions, conversations, SafetyService())
    original = context(uuid.uuid4(), "semantic-context")

    asyncio.run(runtime.handle_message(original, "删掉吧"))

    supplied = sessions.contexts[0]
    assert supplied is not original
    assert [
        (turn.role, turn.text)
        for turn in supplied.authorization_semantic_context
    ] == [
        ("user", "明天下午项目组会几点？"),
        ("assistant", "15:00到16:00。"),
        ("user", "删掉吧"),
    ]
    assert conversations.recent_calls == [
        (
            original.participant_id,
            3,
            {
                "exclude_feishu_message_id": original.message_id,
                "chat_id": original.chat_id,
            },
        )
    ]


def test_conversation_authorization_history_is_chat_scoped():
    database = memory_database()
    person = participant(database, "P-CONTEXT-CHAT")
    events = BotEventRepository(database)
    conversations = ConversationRepository(database)
    now = datetime.now(timezone.utc)

    for marker, chat_id, text in (
        ("a", "chat-a", "项目组会是15点"),
        ("b", "chat-b", "不应跨会话出现"),
    ):
        message_id = f"message-{marker}"
        assert events.accept(
            f"event-{marker}",
            message_id,
            person.id,
            app_id="app",
            open_id="open",
            chat_id=chat_id,
            chat_type="p2p",
            text=text,
            create_time=now,
        )
        conversations.add(
            person.id,
            "assistant",
            text,
            feishu_message_id=message_id,
        )

    recent = conversations.recent(person.id, 4, chat_id="chat-a")

    assert recent == [{"role": "assistant", "content": "项目组会是15点"}]
