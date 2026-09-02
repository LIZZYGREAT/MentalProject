import asyncio
import json
import threading
import time

import pytest

from app.agent.skill_loader import SkillLoader
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.presentation.contracts import AgentActivityEvent
from app.presentation.progress_policy import should_force_silent_progress
from app.presentation.response_orchestrator import ResponseOrchestrator
from app.presentation.semantic_segmenter import SemanticSegmenter
from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
from app.worker import BotWorker
from helpers import memory_database, participant, skill_path


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
                "content": json.dumps({"text": text}),
            },
        },
    }


class RecordingSender:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text, *, message_uuid=None):
        self.sent.append((chat_id, text, message_uuid, time.monotonic()))
        return f"out-{len(self.sent)}"


class IdempotentRecordingSender(RecordingSender):
    def __init__(self):
        super().__init__()
        self.by_uuid = {}

    def send_text(self, chat_id, text, *, message_uuid=None):
        if message_uuid in self.by_uuid:
            return self.by_uuid[message_uuid]
        message_id = super().send_text(
            chat_id, text, message_uuid=message_uuid
        )
        self.by_uuid[message_uuid] = message_id
        return message_id


def _bound_system(
    runtime,
    sender,
    *,
    generic_delay=0.02,
    tool_grace=0.02,
    orchestrator=None,
):
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    identity.bind(
        raw_token=code,
        app_id="cli_test",
        open_id="ou_1",
        chat_id="oc_1",
    )
    events = BotEventRepository(database)
    queue = asyncio.Queue(maxsize=10)
    gateway = FeishuGateway("cli_test", "secret", identity, events, queue)
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        sender,
        model="fake",
        generic_progress_delay_seconds=generic_delay,
        tool_progress_grace_seconds=tool_grace,
        progress_max_messages=4,
        response_orchestrator=orchestrator,
    )
    return gateway, queue, worker, events


async def _send(gateway, queue, worker, *, event_id="event-1", text="request"):
    accepted = gateway.accept_payload(
        payload(event_id, f"message-{event_id}", "ou_1", "oc_1", text)
    )
    assert accepted is True
    await worker.process(await queue.get())


def test_fast_reply_sends_only_one_final_message():
    class FastRuntime:
        async def handle_message(self, *_args, **_kwargs):
            return "final"

    sender = RecordingSender()
    gateway, queue, worker, _ = _bound_system(
        FastRuntime(), sender, generic_delay=0.05, tool_grace=0.05
    )

    asyncio.run(_send(gateway, queue, worker, text="你好"))

    assert [item[1] for item in sender.sent] == ["final"]


@pytest.mark.parametrize(
    "text",
    ["你好", "在吗？", "谢谢", "好的", "收到", "晚安"],
)
def test_slow_social_turn_stays_silent_until_agent_final(text):
    class SlowSocialRuntime:
        async def handle_message(self, *_args, **_kwargs):
            await asyncio.sleep(0.04)
            return "agent final"

    sender = RecordingSender()
    gateway, queue, worker, _ = _bound_system(
        SlowSocialRuntime(), sender, generic_delay=0.01, tool_grace=0.01
    )

    asyncio.run(_send(gateway, queue, worker, text=text))

    assert [item[1] for item in sender.sent] == ["agent final"]


@pytest.mark.parametrize(
    "text",
    ["看下日程", "看下压力", "生成曲线", "好的，那帮我看看下午的日程"],
)
def test_business_requests_are_not_force_silent(text):
    assert should_force_silent_progress(text) is False


@pytest.mark.parametrize("text", ["您好！", "OK.", "嗯嗯。", "早上好？"])
def test_silent_policy_normalizes_case_and_trailing_punctuation(text):
    assert should_force_silent_progress(text) is True


def test_silent_policy_does_not_expand_to_unlisted_social_language():
    assert should_force_silent_progress("你好呀") is False


def test_fast_tool_finishes_inside_tool_grace_without_progress():
    class FastToolRuntime:
        async def handle_message(self, *_args, on_activity, **_kwargs):
            await on_activity(
                AgentActivityEvent(
                    kind="tool_started", tool_name="calendar_list_events"
                )
            )
            await asyncio.sleep(0.005)
            return "final"

    sender = RecordingSender()
    gateway, queue, worker, _ = _bound_system(
        FastToolRuntime(), sender, generic_delay=1, tool_grace=0.05
    )

    asyncio.run(_send(gateway, queue, worker, text="看下日程"))

    assert [item[1] for item in sender.sent] == ["final"]


def test_long_no_tool_turn_sends_one_generic_progress_before_final():
    class LongTextRuntime:
        async def handle_message(self, *_args, **_kwargs):
            await asyncio.sleep(0.04)
            return "final"

    sender = RecordingSender()
    gateway, queue, worker, _ = _bound_system(
        LongTextRuntime(), sender, generic_delay=0.01, tool_grace=1
    )

    asyncio.run(_send(gateway, queue, worker, text="详细解释压力曲线与日程的差异"))

    texts = [item[1] for item in sender.sent]
    assert len(texts) == 2
    assert texts[-1] == "final"
    assert texts[0] != "final"


def test_no_tool_turn_finishing_before_generic_delay_sends_only_final():
    class MediumTextRuntime:
        async def handle_message(self, *_args, **_kwargs):
            await asyncio.sleep(0.01)
            return "final"

    sender = RecordingSender()
    gateway, queue, worker, _ = _bound_system(
        MediumTextRuntime(), sender, generic_delay=0.05, tool_grace=1
    )

    asyncio.run(_send(gateway, queue, worker, text="解释一下这个结果"))

    assert [item[1] for item in sender.sent] == ["final"]


def test_multiple_tool_stages_still_send_at_most_one_progress():
    class MultiToolRuntime:
        async def handle_message(self, *_args, on_activity, **_kwargs):
            await on_activity(
                AgentActivityEvent(
                    kind="tool_started", tool_name="calendar_list_events"
                )
            )
            await asyncio.sleep(0.02)
            await on_activity(
                AgentActivityEvent(
                    kind="tool_started", tool_name="care_run_today_assessment"
                )
            )
            await on_activity(
                AgentActivityEvent(
                    kind="tool_started", tool_name="care_get_pressure_curve"
                )
            )
            await asyncio.sleep(0.02)
            return "final"

    sender = RecordingSender()
    gateway, queue, worker, _ = _bound_system(
        MultiToolRuntime(), sender, generic_delay=1, tool_grace=0.005
    )

    asyncio.run(_send(gateway, queue, worker, text="看看下午压力"))

    texts = [item[1] for item in sender.sent]
    assert len(texts) == 2
    assert texts[-1] == "final"
    assert texts[0] in {
        "我先看看相关日程。",
        "日程信息拿到了，我正在结合这些安排计算压力变化。",
        "我正在结合今天的状态和日程计算压力趋势。",
    }


def test_slow_tool_reply_sends_one_contextual_processing_before_final():
    class SlowRuntime:
        async def handle_message(self, *_args, on_activity, **_kwargs):
            await on_activity(
                AgentActivityEvent(
                    kind="tool_started", tool_name="calendar_list_events"
                )
            )
            await asyncio.sleep(0.05)
            return "final"

    sender = RecordingSender()
    gateway, queue, worker, _ = _bound_system(
        SlowRuntime(), sender, generic_delay=1, tool_grace=0.01
    )

    asyncio.run(_send(gateway, queue, worker))

    texts = [item[1] for item in sender.sent]
    assert texts == ["我先看看相关日程。", "final"]
    assert sender.sent[0][3] < sender.sent[1][3]


def test_threshold_race_waits_for_inflight_processing_send_before_final():
    class RaceRuntime:
        async def handle_message(self, *_args, **_kwargs):
            await asyncio.sleep(0.01)
            return "final"

    class BlockingProgressSender(RecordingSender):
        def __init__(self):
            super().__init__()
            self.progress_started = threading.Event()
            self.release_progress = threading.Event()

        def send_text(self, chat_id, text, *, message_uuid=None):
            if text != "final":
                self.progress_started.set()
                assert self.release_progress.wait(timeout=2)
            return super().send_text(
                chat_id, text, message_uuid=message_uuid
            )

    async def scenario():
        sender = BlockingProgressSender()
        gateway, queue, worker, _ = _bound_system(
            RaceRuntime(), sender, generic_delay=0, tool_grace=1
        )
        task = asyncio.create_task(_send(gateway, queue, worker))
        started = await asyncio.to_thread(sender.progress_started.wait, 1)
        assert started is True
        # The Agent has returned while the provider still owns the processing
        # send. Releasing it must result in processing -> final, never reverse.
        await asyncio.sleep(0.03)
        sender.release_progress.set()
        await task
        return sender

    sender = asyncio.run(scenario())

    texts = [item[1] for item in sender.sent]
    assert len(texts) == 2
    assert texts[0] != "final"
    assert texts[1] == "final"


def test_multi_segment_final_never_allows_late_processing():
    source = (
        "第一段说明复杂任务的结论已经准备好。"
        "第二段补充必要的上下文与边界条件。"
        "第三段给出下一步建议并保持信息完整。"
    )

    class SlowAnalysisRuntime:
        async def handle_message(self, *_args, on_activity, **_kwargs):
            await on_activity(
                AgentActivityEvent(
                    kind="tool_started", tool_name="care_run_today_assessment"
                )
            )
            await asyncio.sleep(0.04)
            return source

    orchestrator = ResponseOrchestrator(
        segmenter=SemanticSegmenter(
            min_total_chars=1,
            target_chars=20,
            max_chars=42,
            max_segments=3,
        )
    )
    sender = RecordingSender()
    gateway, queue, worker, _ = _bound_system(
        SlowAnalysisRuntime(),
        sender,
        generic_delay=1,
        tool_grace=0.01,
        orchestrator=orchestrator,
    )

    asyncio.run(_send(gateway, queue, worker))

    texts = [item[1] for item in sender.sent]
    assert texts[0] == "我正在结合今天的信息进行评估。"
    assert len(texts[1:]) >= 2
    assert "".join(texts[1:]) == source
    assert all("正在" not in text for text in texts[1:])


def test_completed_final_is_not_recovered_as_processing_after_restart():
    class Runtime:
        def __init__(self):
            self.calls = 0

        async def handle_message(self, *_args, **_kwargs):
            self.calls += 1
            return "final"

    runtime = Runtime()
    first_sender = RecordingSender()
    gateway, queue, first_worker, events = _bound_system(
        runtime, first_sender, generic_delay=0.05, tool_grace=0.05
    )
    asyncio.run(_send(gateway, queue, first_worker, event_id="completed"))

    assert events.recoverable() == []
    second_sender = RecordingSender()
    BotWorker(
        queue,
        first_worker.identity,
        events,
        first_worker.runs,
        first_worker.skill_loader,
        runtime,
        second_sender,
        model="fake",
        generic_progress_delay_seconds=0,
        tool_progress_grace_seconds=0,
    )
    duplicate_accepted = gateway.accept_payload(
        payload("completed", "message-completed", "ou_1", "oc_1", "request")
    )

    assert duplicate_accepted is False
    assert queue.empty()
    assert second_sender.sent == []
    assert runtime.calls == 1


def test_restart_during_agent_turn_reuses_one_event_level_processing_message():
    class InterruptedRuntime:
        async def handle_message(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    class RecoveredRuntime:
        async def handle_message(self, *_args, on_activity, **_kwargs):
            await on_activity(
                AgentActivityEvent(
                    kind="tool_started", tool_name="care_get_pressure_curve"
                )
            )
            await asyncio.sleep(0.02)
            return "final"

    async def scenario():
        sender = IdempotentRecordingSender()
        gateway, queue, first_worker, events = _bound_system(
            InterruptedRuntime(), sender, generic_delay=0, tool_grace=0
        )
        first = asyncio.create_task(
            _send(gateway, queue, first_worker, event_id="restart-active")
        )
        for _ in range(100):
            if sender.sent:
                break
            await asyncio.sleep(0.005)
        assert len(sender.sent) == 1
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

        recovered = events.recoverable()
        assert [item.event_id for item in recovered] == ["restart-active"]
        second_worker = BotWorker(
            queue,
            first_worker.identity,
            events,
            first_worker.runs,
            first_worker.skill_loader,
            RecoveredRuntime(),
            sender,
            model="fake",
            generic_progress_delay_seconds=0,
            tool_progress_grace_seconds=0,
            progress_max_messages=1,
        )
        item = recovered[0]
        from app.integrations.feishu.gateway import BotEvent

        await second_worker.process(
            BotEvent(
                item.event_id,
                item.message_id,
                item.app_id,
                item.open_id,
                item.chat_id,
                item.text,
                item.create_time,
                item.chat_type,
            )
        )
        return sender

    sender = asyncio.run(scenario())

    visible_texts = [item[1] for item in sender.sent]
    assert visible_texts[-1] == "final"
    assert len(visible_texts) == 2
    assert len({item[2] for item in sender.sent[:-1]}) == 1
