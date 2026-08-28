import asyncio

from app.agent.skill_loader import SkillLoader
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.presentation.contracts import AgentActivityEvent
from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
from app.worker import BotWorker
from helpers import memory_database, participant, skill_path
from app.repositories import ParticipantRepository


class FakeRuntime:
    def __init__(self):
        self.seen = []

    async def handle_message(self, ctx, text, **_kwargs):
        self.seen.append((ctx.participant_id, ctx.participant_code, text))
        return f"{ctx.participant_code}:{text}"


class FakeSender:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return f"out-{len(self.sent)}"


class FailOnceSender(FakeSender):
    def __init__(self):
        super().__init__()
        self.failed = False

    def send_text(self, chat_id, text):
        from app.integrations.feishu.client import FeishuSendError

        if not self.failed:
            self.failed = True
            raise FeishuSendError("temporary", retryable=False)
        return super().send_text(chat_id, text)


def payload(event_id, message_id, open_id, chat_id, text):
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": open_id}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": "text",
                "content": __import__("json").dumps({"text": text}),
            },
        },
    }


def test_two_users_bind_route_and_run_without_identity_crossover():
    database = memory_database()
    p1 = participant(database, "P001")
    p2 = participant(database, "P002")
    bindings = BindingRepository(database)
    identity = IdentityService(database, bindings)
    code1, _ = identity.create_invite(p1.id)
    code2, _ = identity.create_invite(p2.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=10)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    runtime = FakeRuntime()
    sender = FakeSender()
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        sender,
        model="fake",
    )

    async def scenario():
        assert gateway.accept_payload(payload("e1", "m1", "ou_1", "oc_1", f"/bind {code1}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("e2", "m2", "ou_2", "oc_2", f"/bind {code2}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("e3", "m3", "ou_1", "oc_1", "apple"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("e4", "m4", "ou_2", "oc_2", "banana"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert runtime.seen == [
        (p1.id, "P001", "apple"),
        (p2.id, "P002", "banana"),
    ]
    assert sender.sent[-2:] == [("oc_1", "P001:apple"), ("oc_2", "P002:banana")]


def test_staged_reply_is_sent_after_worker_restart_without_rerunning_agent(caplog):
    database = memory_database()
    p1 = participant(database, "P001")
    bindings = BindingRepository(database)
    identity = IdentityService(database, bindings)
    code, _ = identity.create_invite(p1.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=10)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    runtime = FakeRuntime()
    first_sender = FailOnceSender()
    first_worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        first_sender,
        model="fake",
        max_retries=0,
    )

    async def scenario():
        assert gateway.accept_payload(payload("b1", "bm1", "ou_1", "oc_1", f"/bind {code}"))
        with caplog.at_level("WARNING"):
            await first_worker.process(await queue.get())
        # The first send failed, but the successful binding reply is durable.
        saved = events.recoverable()
        assert saved[0].event_id == "b1"
        second_sender = FakeSender()
        second_worker = BotWorker(
            queue,
            identity,
            events,
            AgentRunRepository(database),
            SkillLoader(skill_path()),
            runtime,
            second_sender,
            model="fake",
        )
        recovered = saved[0]
        from app.integrations.feishu.gateway import BotEvent

        await second_worker.process(
            BotEvent(
                recovered.event_id,
                recovered.message_id,
                recovered.app_id,
                recovered.open_id,
                recovered.chat_id,
                recovered.text,
                recovered.create_time,
                recovered.chat_type,
            )
        )
        assert second_sender.sent == [("oc_1", "绑定成功：P001")]
        assert runtime.seen == []

    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    assert any("feishu_reply_send_failed" in message for message in messages)
    assert "oc_1" not in " ".join(messages)


def test_same_participant_messages_are_processed_serially():
    database = memory_database()
    p1 = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(p1.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=10)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)

    class SerialRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.locks = {}

        async def handle_message(self, ctx, text, **_kwargs):
            lock = self.locks.setdefault(ctx.participant_id, asyncio.Lock())
            async with lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.02)
                self.seen.append((ctx.participant_id, ctx.participant_code, text))
                self.active -= 1
                return text

    runtime = SerialRuntime()
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        FakeSender(),
        model="fake",
    )

    async def scenario():
        assert gateway.accept_payload(payload("s1", "sm1", "ou_1", "oc_1", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("s2", "sm2", "ou_1", "oc_1", "first"))
        assert gateway.accept_payload(payload("s3", "sm3", "ou_1", "oc_1", "second"))
        first = await queue.get()
        second = await queue.get()
        await asyncio.gather(worker.process(first), worker.process(second))

    asyncio.run(scenario())
    assert runtime.max_active == 1
    assert [item[2] for item in runtime.seen] == ["first", "second"]


def test_external_llm_is_blocked_until_research_consent_is_recorded():
    database = memory_database()
    p1 = ParticipantRepository(database).create("P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(p1.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=10)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    runtime = FakeRuntime()
    sender = FakeSender()
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        sender,
        model="fake",
    )

    async def scenario():
        assert gateway.accept_payload(payload("c1", "cm1", "ou_1", "oc_1", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("c2", "cm2", "ou_1", "oc_1", "hello"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert runtime.seen == []
    assert "实验授权" in sender.sent[-1][1]


def test_stop_bypasses_running_turn_and_interrupts_runtime():
    from app.agent.claude_runtime import ClaudeRuntimeInterrupted

    database = memory_database()
    p1 = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(p1.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=10)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)

    class InterruptRuntime:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()

        async def handle_message(self, _ctx, _text, **_kwargs):
            self.started.set()
            await self.stopped.wait()
            raise ClaudeRuntimeInterrupted("stopped")

        async def interrupt(self, participant_id):
            assert participant_id == p1.id
            self.stopped.set()
            return True

    runtime = InterruptRuntime()
    sender = FakeSender()
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        sender,
        model="fake",
        progress_delay_seconds=60,
    )

    async def scenario():
        assert gateway.accept_payload(payload("x1", "xm1", "ou_1", "oc_1", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("x2", "xm2", "ou_1", "oc_1", "long"))
        running = asyncio.create_task(worker.process(await queue.get()))
        await runtime.started.wait()
        assert gateway.accept_payload(payload("x3", "xm3", "ou_1", "oc_1", "/stop"))
        await worker.process(await queue.get())
        await running

    asyncio.run(scenario())
    texts = [text for _chat, text in sender.sent]
    assert "已请求停止当前处理。" in texts
    assert "当前处理已停止。" in texts


def test_fast_tool_reply_finishes_inside_grace_without_processing_message():
    database = memory_database()
    p1 = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(p1.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=10)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)

    class ProgressRuntime:
        async def handle_message(self, _ctx, _text, *, on_activity, **_kwargs):
            await on_activity(
                AgentActivityEvent(
                    kind="tool_started", tool_name="care_run_today_assessment"
                )
            )
            await on_activity(
                AgentActivityEvent(
                    kind="tool_succeeded",
                    tool_name="care_run_today_assessment",
                    status="succeeded",
                )
            )
            return "final assessment"

    sender = FakeSender()
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        ProgressRuntime(),
        sender,
        model="fake",
        progress_delay_seconds=60,
        progress_cooldown_seconds=1,
        progress_max_messages=2,
    )

    async def scenario():
        assert gateway.accept_payload(payload("p1", "pm1", "ou_1", "oc_1", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("p2", "pm2", "ou_1", "oc_1", "assess"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert sender.sent[-1:] == [("oc_1", "final assessment")]
    assert all("评估" not in text for _chat, text in sender.sent[:-1])
