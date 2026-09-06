import asyncio
import json
from types import SimpleNamespace

from app.agent.skill_loader import SkillLoader
from app.contracts.generic_image_context import GenericImageContext
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.presentation.contracts import RuntimeResponse
from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
from app.services.generic_image_vision import GenericImageVisionUnavailable
from app.worker import BotWorker, parse_schedule_correction
from helpers import memory_database, participant, skill_path


class Sender:
    def __init__(self):
        self.texts = []

    def send_text(self, _chat_id, text, **_kwargs):
        self.texts.append(text)
        return f"reply-{len(self.texts)}"


class Runtime:
    def __init__(self):
        self.calls = []

    async def handle_message(self, ctx, turn_input, **_kwargs):
        self.calls.append((ctx, turn_input))
        return RuntimeResponse(text=f"answer:{turn_input.text or 'image-only'}")

    async def interrupt(self, _participant_id):
        return False


class Resources:
    def __init__(self):
        self.calls = []

    async def download_image(self, message_id, image_key):
        self.calls.append((message_id, image_key))
        return SimpleNamespace(data=b"\x89PNG\r\n\x1a\n", mime_type="image/png")


class Vision:
    model = "generic-vision"

    def __init__(self, kind="code_or_error_screenshot", *, delay=0, failure=False):
        self.kind = kind
        self.delay = delay
        self.failure = failure
        self.calls = []

    async def inspect(self, _data, _mime, *, user_text=""):
        self.calls.append(user_text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise GenericImageVisionUnavailable("timeout")
        return GenericImageContext(
            image_kind=self.kind,
            summary="图片摘要",
            visible_text="可见文字",
        )


def test_natural_schedule_correction_phrases_are_parsed_without_schema_terms():
    assert parse_schedule_correction("高数其实是第3-4节") == {
        "course_name": "高数",
        "period_start": 3,
        "period_end": 4,
    }
    assert parse_schedule_correction("周三那门课是单周") == {
        "weekday": 3,
        "odd_even": "odd",
    }
    assert parse_schedule_correction("这里是逸夫楼") == {"location": "逸夫楼"}


def _payload(event_id, message_id, message_type, *, text=""):
    content = {"text": text} if message_type == "text" else {"image_key": "img-key"}
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "open"}},
            "message": {
                "message_id": message_id,
                "chat_id": "chat",
                "chat_type": "p2p",
                "message_type": message_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        },
    }


def _system(*, vision=None, debounce=0.2, association=1.0, recent=1.0):
    database = memory_database()
    person = participant(database, "MM-001")
    bindings = BindingRepository(database)
    identity = IdentityService(database, bindings)
    code, _ = identity.create_invite(person.id)
    identity.bind(
        raw_token=code,
        app_id="app",
        open_id="open",
        chat_id="chat",
    )
    queue = asyncio.Queue()
    events = BotEventRepository(database)
    gateway = FeishuGateway("app", "secret", identity, events, queue)
    runtime = Runtime()
    sender = Sender()
    resources = Resources()
    vision = vision or Vision()
    worker = BotWorker(
        queue,
        identity,
        events,
        AgentRunRepository(database),
        SkillLoader(skill_path()),
        runtime,
        sender,
        model="fake",
        generic_image_vision=vision,
        message_resources=resources,
        multimodal_debounce_seconds=debounce,
        multimodal_association_seconds=association,
        multimodal_recent_context_seconds=recent,
    )
    return gateway, queue, worker, runtime, sender, vision, resources


def test_image_plus_nearby_text_is_one_turn_and_one_final_reply():
    gateway, queue, worker, runtime, sender, vision, _ = _system()

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("text", "m-text", "text", text="看看是什么问题")
        )
        await worker.process(await queue.get())
        await image_task

    asyncio.run(scenario())
    assert vision.calls == ["看看是什么问题"]
    assert len(runtime.calls) == 1
    assert runtime.calls[0][1].text == "看看是什么问题"
    assert runtime.calls[0][0].calendar_mutation_allowed is False
    assert sender.texts == ["answer:看看是什么问题"]


def test_image_only_uses_read_only_agent_and_never_assumes_import():
    gateway, queue, worker, runtime, sender, _, _ = _system(debounce=0)

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(runtime.calls) == 1
    assert runtime.calls[0][1].text == ""
    assert runtime.calls[0][0].calendar_mutation_allowed is False
    assert sender.texts == ["answer:image-only"]


def test_explicit_schedule_import_is_fast_path_without_generic_vision_or_agent():
    vision = Vision(failure=True)
    gateway, queue, worker, runtime, sender, _, _ = _system(vision=vision)
    strict_calls = []

    async def strict(event, participant_id, **_kwargs):
        strict_calls.append((event.message_id, participant_id))
        await worker._deliver(event, "strict-preview")

    worker._handle_schedule_image = strict

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("intent", "m-intent", "text", text="把这个课程表导入飞书日历")
        )
        await worker.process(await queue.get())
        await image_task

    asyncio.run(scenario())
    assert len(strict_calls) == 1
    assert vision.calls == []
    assert runtime.calls == []
    assert sender.texts == ["strict-preview"]


def test_course_schedule_question_is_read_only_and_recent_followup_reuses_context():
    gateway, queue, worker, runtime, sender, vision, resources = _system(
        vision=Vision(kind="course_schedule")
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        await asyncio.sleep(0.005)
        assert gateway.accept_payload(
            _payload("question", "m-question", "text", text="周三有什么课？")
        )
        await worker.process(await queue.get())
        await image_task
        assert gateway.accept_payload(
            _payload("followup", "m-followup", "text", text="那周四呢？")
        )
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(vision.calls) == 1
    assert len(resources.calls) == 1
    assert [call[1].text for call in runtime.calls] == ["周三有什么课？", "那周四呢？"]
    assert all(call[0].calendar_mutation_allowed is False for call in runtime.calls)
    assert len(sender.texts) == 2


def test_vision_failure_with_attached_text_produces_one_consistent_reply():
    vision = Vision(delay=0.04, failure=True)
    gateway, queue, worker, runtime, sender, _, _ = _system(
        vision=vision, debounce=0, association=0.2
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        await asyncio.sleep(0.01)
        assert gateway.accept_payload(
            _payload("text", "m-text", "text", text="按默认学校作息")
        )
        await worker.process(await queue.get())
        await image_task

    asyncio.run(scenario())
    assert runtime.calls == []
    assert sender.texts == [
        "这张图刚才没有读完整，你可以重发一次；如果方便，也可以告诉我你想让我重点看哪里。"
    ]


def test_unrelated_text_after_association_window_is_a_new_agent_turn():
    gateway, queue, worker, runtime, sender, _, _ = _system(
        debounce=0, association=0.01
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        await worker.process(await queue.get())
        await asyncio.sleep(0.02)
        assert gateway.accept_payload(
            _payload("text", "m-text", "text", text="今天心情不错")
        )
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(runtime.calls) == 2
    assert runtime.calls[0][0].calendar_mutation_allowed is False
    assert runtime.calls[1][0].calendar_mutation_allowed is True
    assert len(sender.texts) == 2


def test_stop_passes_routing_lock_and_cancels_long_image_turn():
    vision = Vision(delay=1.0)
    gateway, queue, worker, runtime, sender, _, _ = _system(
        vision=vision, debounce=0, association=1.0
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(200):
            if vision.calls:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("stop", "m-stop", "text", text="/stop")
        )
        await worker.process(await queue.get())
        await asyncio.gather(image_task, return_exceptions=True)

    asyncio.run(scenario())
    assert runtime.calls == []
    assert sender.texts == ["已请求停止当前处理。"]
