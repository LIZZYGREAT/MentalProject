import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import threading
import time
import uuid
import ast
from pathlib import Path

from app.agent.context import AgentContext
from app.agent.sdk_adapter import ClaudeTurnResult
from app.agent.session_manager import ParticipantSessionManager
from app.integrations.feishu.gateway import BotEvent
from app.worker import BotWorker


class SlowIdentity:
    def __init__(self):
        self.started = threading.Event()

    def resolve(self, _app_id, _open_id):
        self.started.set()
        time.sleep(0.3)
        return None


class ReplyEvents:
    def __init__(self):
        self.plan = None

    def pending_reply_plan(self, _event_id):
        return self.plan

    def set_processing(self, _event_id, _participant_id):
        return None

    def stage_reply_plan(self, _event_id, *, full_text, segments):
        self.plan = SimpleNamespace(
            full_text=full_text,
            segments=tuple(segments),
            next_segment=0,
        )

    def mark_reply_segment_sent(self, _event_id, *, segment_index, message_id):
        self.plan.next_segment = segment_index + 1

    def finish_reply_plan(self, _event_id):
        return None


class Sender:
    def send_text(self, _chat_id, _text, *, message_uuid=None):
        return message_uuid or "message-1"


def test_bot_worker_slow_identity_repository_does_not_block_event_loop():
    async def scenario():
        identity = SlowIdentity()
        worker = BotWorker(
            asyncio.Queue(),
            identity,
            ReplyEvents(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            Sender(),
            model="test-model",
        )
        event = BotEvent(
            "event-1",
            "message-1",
            "app-1",
            "open-1",
            "chat-1",
            "你好",
            datetime.now(timezone.utc),
        )
        processing = asyncio.create_task(worker.process(event))
        assert await asyncio.to_thread(identity.started.wait, 1.0)

        started = time.monotonic()
        await asyncio.sleep(0.05)
        heartbeat_elapsed = time.monotonic() - started

        assert heartbeat_elapsed < 0.2
        await processing

    asyncio.run(scenario())


class SlowSessionRepository:
    def __init__(self):
        self.get_started = threading.Event()
        self.saved = []

    def get(self, _participant_id):
        self.get_started.set()
        time.sleep(0.3)
        return None

    def save(self, participant_id, session_id, *, last_message_id):
        time.sleep(0.05)
        self.saved.append((participant_id, session_id, last_message_id))

    def mark_stale(self, _participant_id):
        return None


class ImmediateClient:
    def __init__(self, binding):
        self.binding = binding

    async def connect(self):
        return None

    async def run_turn(self, text):
        return ClaudeTurnResult(f"answer:{text}", "session-1")

    async def interrupt(self):
        return None

    async def disconnect(self):
        return None


class ImmediateFactory:
    def create(self, binding, *, resume_session_id):
        assert resume_session_id is None
        return ImmediateClient(binding)


def test_session_manager_slow_repository_get_does_not_block_event_loop():
    async def scenario():
        repository = SlowSessionRepository()
        manager = ParticipantSessionManager(
            ImmediateFactory(), repository, idle_timeout_seconds=60
        )
        participant_id = uuid.uuid4()
        context = AgentContext(
            participant_id,
            "P001",
            "open-1",
            "chat-1",
            "message-1",
            uuid.uuid4(),
        )
        turn = asyncio.create_task(manager.submit(context, "hello"))
        assert await asyncio.to_thread(repository.get_started.wait, 1.0)

        started = time.monotonic()
        await asyncio.sleep(0.05)
        heartbeat_elapsed = time.monotonic() - started

        assert heartbeat_elapsed < 0.2
        result = await turn
        assert result.text == "answer:hello"
        assert repository.saved == [
            (participant_id, "session-1", "message-1")
        ]
        await manager.close()

    asyncio.run(scenario())


def test_device_flow_async_methods_do_not_execute_database_sessions_inline():
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "integrations"
        / "feishu"
        / "oauth.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {"start", "poll_until_complete"}
    }

    assert set(methods) == {"start", "poll_until_complete"}
    for method in methods.values():
        inline_sessions = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "session"
        ]
        assert inline_sessions == []
