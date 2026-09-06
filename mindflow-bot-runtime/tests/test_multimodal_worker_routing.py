import asyncio
import gc
import json
from types import SimpleNamespace
import weakref

from app.agent.skill_loader import SkillLoader
from app.agent.claude_runtime import ClaudeAgentRuntime
from app.agent.sdk_adapter import ClaudeSDKTurnInterrupted, ClaudeTurnResult
from app.agent.session_manager import ParticipantSessionManager
from app.contracts.course_schedule import ScheduleVisionResult
from app.contracts.generic_image_context import GenericImageContext
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.models import AgentRun, BotEvent as StoredBotEvent
from app.presentation.contracts import RuntimeResponse
from app.repositories import (
    AgentRunRepository,
    BindingRepository,
    BotEventRepository,
    ClaudeSessionRepository,
    ConversationRepository,
)
from app.services.safety_service import SafetyService
from app.services.generic_image_vision import GenericImageVisionUnavailable
from app.worker import (
    BotWorker,
    ScheduleImageOutcome,
    is_direct_image_calendar_request,
    is_strong_schedule_import_intent,
    parse_schedule_correction,
)
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


class CancellableRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.pending = []

    async def handle_message(self, ctx, turn_input, **_kwargs):
        self.calls.append((ctx, turn_input))
        future = asyncio.get_running_loop().create_future()
        self.pending.append(future)
        self.started.set()
        await future
        raise AssertionError("cancelled runtime must not resume")

    async def interrupt(self, _participant_id):
        cancelled = False
        for future in self.pending:
            if not future.done():
                future.cancel()
                cancelled = True
        return cancelled


class ProductionStyleClient:
    def __init__(self, binding, resume, factory):
        self.binding = binding
        self.resume = resume
        self.factory = factory
        self.release = asyncio.Event()
        self.interrupted = False

    async def connect(self):
        return None

    async def run_turn(self, turn_input):
        self.factory.started.set()
        await self.release.wait()
        if self.interrupted:
            raise ClaudeSDKTurnInterrupted("interrupted")
        return ClaudeTurnResult(f"answer:{turn_input.text}", "session")

    async def interrupt(self):
        self.interrupted = True
        self.release.set()

    async def disconnect(self):
        return None


class ProductionStyleFactory:
    def __init__(self):
        self.started = asyncio.Event()

    def create(self, binding, *, resume_session_id):
        return ProductionStyleClient(binding, resume_session_id, self)


class FailingInterruptProductionClient(ProductionStyleClient):
    async def interrupt(self):
        raise RuntimeError("interrupt transport failed")


class FailingInterruptProductionFactory(ProductionStyleFactory):
    def create(self, binding, *, resume_session_id):
        return FailingInterruptProductionClient(
            binding, resume_session_id, self
        )


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


def _system(
    *,
    vision=None,
    runtime=None,
    schedule_vision=None,
    schedule_imports=None,
    debounce=0.2,
    association=1.0,
    recent=1.0,
):
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
    runtime = runtime or Runtime()
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
        schedule_vision=schedule_vision,
        schedule_imports=schedule_imports,
        message_resources=resources,
        multimodal_debounce_seconds=debounce,
        multimodal_association_seconds=association,
        multimodal_recent_context_seconds=recent,
    )
    return gateway, queue, worker, runtime, sender, vision, resources


def _strict_schedule_result():
    return ScheduleVisionResult.from_dict({
        "document_type": "course_schedule",
        "semester_label": "2026 秋",
        "institution": "测试大学",
        "courses": [{
            "course_name": "高等数学",
            "weekday": 3,
            "period_start": 7,
            "period_end": 8,
            "start_time": None,
            "end_time": None,
            "location": "教一楼",
            "teacher": "张老师",
            "week_rule": {
                "start_week": 1,
                "end_week": 16,
                "odd_even": "all",
                "explicit_weeks": None,
            },
            "uncertain_fields": [],
            "period_inference_source": "grid_position",
            "period_confidence": 0.95,
        }],
        "missing_context": [],
        "warnings": [],
    })


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


def test_generic_image_bytes_are_released_before_main_agent_wait():
    class ImagePayload:
        __slots__ = ("data", "mime_type", "__weakref__")

        def __init__(self):
            self.data = b"large-image-bytes"
            self.mime_type = "image/png"

    class TrackingResources(Resources):
        async def download_image(self, message_id, image_key):
            self.calls.append((message_id, image_key))
            payload = ImagePayload()
            self.payload_ref = weakref.ref(payload)
            return payload

    class VerifyingRuntime(Runtime):
        async def handle_message(self, ctx, turn_input, **_kwargs):
            gc.collect()
            assert resources.payload_ref() is None
            return await super().handle_message(ctx, turn_input, **_kwargs)

    resources = TrackingResources()
    runtime = VerifyingRuntime()
    gateway, queue, worker, _, _sender, _, _ = _system(
        runtime=runtime, vision=Vision(kind="photo"), debounce=0
    )
    worker.message_resources = resources

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(runtime.calls) == 1


def test_false_positive_fallback_releases_image_before_agent_wait():
    class ImagePayload:
        __slots__ = ("data", "mime_type", "__weakref__")

        def __init__(self):
            self.data = b"large-image-bytes"
            self.mime_type = "image/png"

    class TrackingResources(Resources):
        async def download_image(self, message_id, image_key):
            self.calls.append((message_id, image_key))
            payload = ImagePayload()
            self.payload_ref = weakref.ref(payload)
            return payload

    class StrictVision:
        model = "strict-vision"

        async def parse(self, _data, _mime):
            return ScheduleVisionResult.from_dict({
                "document_type": "not_course_schedule",
                "semester_label": None,
                "institution": None,
                "courses": [],
                "missing_context": [],
                "warnings": [],
            })

    class Drafts:
        def get_by_source(self, _participant_id, _message_id):
            return None

    class VerifyingRuntime(Runtime):
        async def handle_message(self, ctx, turn_input, **_kwargs):
            gc.collect()
            assert resources.payload_ref() is None
            return await super().handle_message(ctx, turn_input, **_kwargs)

    resources = TrackingResources()
    runtime = VerifyingRuntime()
    gateway, queue, worker, _, _sender, _, _ = _system(
        runtime=runtime,
        vision=Vision(kind="course_schedule"),
        schedule_vision=StrictVision(),
        schedule_imports=SimpleNamespace(drafts=Drafts()),
        debounce=0.2,
    )
    worker.message_resources = resources

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload(
                "intent",
                "m-intent",
                "text",
                text="把这个讲座添加到日历",
            )
        )
        await worker.process(await queue.get())
        await image_task

    asyncio.run(scenario())
    assert len(runtime.calls) == 1


def test_explicit_import_to_calendar_still_uses_fast_path():
    vision = Vision(failure=True)
    gateway, queue, worker, runtime, sender, _, _ = _system(vision=vision)
    strict_calls = []

    async def strict(event, participant_id, **_kwargs):
        strict_calls.append((event.message_id, participant_id))
        await worker._deliver(event, "strict-preview")
        return ScheduleImageOutcome(
            "draft_created", {"id": "draft-1"}, "course_schedule"
        )

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


def test_text_arriving_during_vision_failure_is_not_silently_lost():
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
    assert [call[1].text for call in runtime.calls] == ["按默认学校作息"]
    assert sender.texts == [
        "这张图刚才没有读完整，你可以重发一次；如果方便，也可以告诉我你想让我重点看哪里。",
        "answer:按默认学校作息",
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


def test_explicit_reference_after_new_image_association_timeout_never_reuses_old_image():
    class SecondImageBlocks(Vision):
        def __init__(self):
            super().__init__(kind="photo")
            self.second_started = asyncio.Event()
            self.release_second = asyncio.Event()

        async def inspect(self, data, mime, *, user_text=""):
            self.calls.append(user_text)
            if len(self.calls) == 2:
                self.second_started.set()
                await self.release_second.wait()
            return GenericImageContext(
                image_kind="photo",
                summary="图片摘要",
                visible_text="可见文字",
            )

    vision = SecondImageBlocks()
    gateway, queue, worker, runtime, _sender, _, _resources = _system(
        vision=vision, debounce=0, association=0.01
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image-1", "m-image-1", "image"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(_payload("image-2", "m-image-2", "image"))
        second = asyncio.create_task(worker.process(await queue.get()))
        await vision.second_started.wait()
        await asyncio.sleep(0.02)
        assert gateway.accept_payload(
            _payload("question", "m-question", "text", text="这张图怎么看？")
        )
        await worker.process(await queue.get())
        vision.release_second.set()
        await second

    asyncio.run(scenario())
    question_call = next(call for call in runtime.calls if call[1].text == "这张图怎么看？")
    assert question_call[1].trusted_image_context is None


def test_slow_first_image_fast_second_image_followup_uses_second_image():
    class MessageAwareResources(Resources):
        async def download_image(self, message_id, image_key):
            self.calls.append((message_id, image_key))
            return SimpleNamespace(
                data=message_id.encode(), mime_type="image/png"
            )

    class MessageAwareVision(Vision):
        async def inspect(self, data, _mime, *, user_text=""):
            message_id = data.decode()
            self.calls.append(message_id)
            return GenericImageContext(
                image_kind="photo",
                summary=message_id,
                visible_text="",
            )

    class SlowFirstRuntime(Runtime):
        def __init__(self):
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def handle_message(self, ctx, turn_input, **_kwargs):
            self.calls.append((ctx, turn_input))
            trusted = turn_input.trusted_image_context or {}
            if trusted.get("summary") == "m-image-1":
                self.first_started.set()
                await self.release_first.wait()
            return RuntimeResponse(text=f"answer:{turn_input.text or 'image-only'}")

    runtime = SlowFirstRuntime()
    gateway, queue, worker, _, _sender, _, _ = _system(
        runtime=runtime, vision=MessageAwareVision(), debounce=0, association=0.01
    )
    worker.message_resources = MessageAwareResources()

    async def scenario():
        assert gateway.accept_payload(_payload("image-1", "m-image-1", "image"))
        first = asyncio.create_task(worker.process(await queue.get()))
        await runtime.first_started.wait()
        assert gateway.accept_payload(_payload("image-2", "m-image-2", "image"))
        await worker.process(await queue.get())
        runtime.release_first.set()
        await first
        await asyncio.sleep(0.02)
        assert gateway.accept_payload(
            _payload("followup", "m-followup", "text", text="这张图怎么看？")
        )
        await worker.process(await queue.get())

    asyncio.run(scenario())
    followup = next(call for call in runtime.calls if call[1].text == "这张图怎么看？")
    assert followup[1].trusted_image_context["summary"] == "m-image-2"


def test_six_second_style_supplement_reuses_image_even_after_fast_completion():
    gateway, queue, worker, runtime, sender, vision, resources = _system(
        vision=Vision(kind="course_schedule"), debounce=0, association=1.0
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(
            _payload("supplement", "m-supplement", "text", text="按默认学校作息")
        )
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(vision.calls) == 1
    assert len(resources.calls) == 1
    assert [call[1].text for call in runtime.calls] == ["", "按默认学校作息"]
    assert all(call[0].calendar_mutation_allowed is False for call in runtime.calls)
    assert len(sender.texts) == 2


def test_two_images_are_two_image_only_turns_not_one_aggregate():
    gateway, queue, worker, runtime, sender, vision, resources = _system(
        debounce=0.2, association=1.0
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image-1", "m-image-1", "image"))
        first = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(_payload("image-2", "m-image-2", "image"))
        second = asyncio.create_task(worker.process(await queue.get()))
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert len(vision.calls) == 2
    assert len(resources.calls) == 2
    assert len(runtime.calls) == 2
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


def _agent_run_states(worker):
    with worker.runs.database.session() as session:
        return [
            (row.status, row.finished_at)
            for row in session.query(AgentRun).order_by(AgentRun.started_at).all()
        ]


def test_cancelled_queued_agent_run_is_finished_as_interrupted():
    runtime = CancellableRuntime()
    gateway, queue, worker, _, sender, _, _ = _system(runtime=runtime)

    async def scenario():
        assert gateway.accept_payload(
            _payload("text", "m-text", "text", text="排队请求")
        )
        agent_task = asyncio.create_task(worker.process(await queue.get()))
        await runtime.started.wait()
        assert gateway.accept_payload(_payload("stop", "m-stop", "text", text="/stop"))
        await worker.process(await queue.get())
        await asyncio.gather(agent_task, return_exceptions=True)

    asyncio.run(scenario())
    states = _agent_run_states(worker)
    assert [status for status, _finished_at in states] == ["interrupted"]
    assert states[0][1] is not None
    with worker.events.database.session() as session:
        assert session.get(StoredBotEvent, "text").status == "interrupted"
    assert sender.texts == ["已请求停止当前处理。"]


def test_cancelled_active_multimodal_agent_run_is_finished_as_interrupted():
    runtime = CancellableRuntime()
    gateway, queue, worker, _, _sender, _, _ = _system(
        runtime=runtime, debounce=0
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        await runtime.started.wait()
        assert gateway.accept_payload(_payload("stop", "m-stop", "text", text="/stop"))
        await worker.process(await queue.get())
        await asyncio.gather(image_task, return_exceptions=True)

    asyncio.run(scenario())
    states = _agent_run_states(worker)
    assert [status for status, _finished_at in states] == ["interrupted"]
    assert states[0][1] is not None
    with worker.events.database.session() as session:
        assert session.get(StoredBotEvent, "image").status == "interrupted"


def test_stop_does_not_leave_agent_run_running():
    runtime = CancellableRuntime()
    gateway, queue, worker, _, _sender, _, _ = _system(
        runtime=runtime, debounce=0
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        task = asyncio.create_task(worker.process(await queue.get()))
        await runtime.started.wait()
        assert gateway.accept_payload(_payload("stop", "m-stop", "text", text="/stop"))
        await worker.process(await queue.get())
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert all(status != "running" for status, _finished_at in _agent_run_states(worker))


def test_stop_does_not_emit_duplicate_interrupted_reply():
    runtime = CancellableRuntime()
    gateway, queue, worker, _, sender, _, _ = _system(
        runtime=runtime, debounce=0
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        task = asyncio.create_task(worker.process(await queue.get()))
        await runtime.started.wait()
        assert gateway.accept_payload(_payload("stop", "m-stop", "text", text="/stop"))
        await worker.process(await queue.get())
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert sender.texts == ["已请求停止当前处理。"]


def _run_production_style_stop(*, factory=None, message_type="text"):
    gateway, queue, worker, _runtime, sender, _, _ = _system()
    database = worker.runs.database
    factory = factory or ProductionStyleFactory()
    sessions = ParticipantSessionManager(
        factory,
        ClaudeSessionRepository(database),
        idle_timeout_seconds=60,
    )
    worker.runtime = ClaudeAgentRuntime(
        sessions,
        ConversationRepository(database),
        SafetyService(),
    )

    async def scenario():
        original_event_id = f"production-{message_type}"
        assert gateway.accept_payload(
            _payload(
                original_event_id,
                f"m-{original_event_id}",
                message_type,
                text="长任务" if message_type == "text" else "",
            )
        )
        agent_task = asyncio.create_task(worker.process(await queue.get()))
        await factory.started.wait()
        assert gateway.accept_payload(
            _payload("production-stop", "m-production-stop", "text", text="/stop")
        )
        await worker.process(await queue.get())
        await asyncio.gather(agent_task, return_exceptions=True)
        await worker.runtime.close()

    asyncio.run(scenario())
    return worker, sender


def test_production_style_stop_emits_exactly_one_user_visible_stop_reply():
    _worker, sender = _run_production_style_stop()

    assert sender.texts == ["已请求停止当前处理。"]


def test_production_style_stop_finishes_agent_run_as_interrupted():
    worker, _sender = _run_production_style_stop()

    states = _agent_run_states(worker)
    assert [status for status, _finished_at in states] == ["interrupted"]
    assert states[0][1] is not None


def test_production_style_stop_marks_original_bot_event_interrupted():
    worker, _sender = _run_production_style_stop()

    with worker.events.database.session() as session:
        event = session.get(StoredBotEvent, "production-text")
        assert event.status == "interrupted"
        assert event.error_code == "stopped"


def test_stop_cleanup_continues_when_client_interrupt_raises():
    worker, _sender = _run_production_style_stop(
        factory=FailingInterruptProductionFactory(),
        message_type="image",
    )

    assert worker._active_multimodal_tasks == {}
    with worker.events.database.session() as session:
        assert session.get(StoredBotEvent, "production-image").status == "interrupted"


def test_stop_reply_is_still_sent_when_sdk_interrupt_fails():
    _worker, sender = _run_production_style_stop(
        factory=FailingInterruptProductionFactory()
    )

    assert sender.texts == ["已请求停止当前处理。"]


def test_stop_interrupt_failure_does_not_leave_agent_run_running():
    worker, _sender = _run_production_style_stop(
        factory=FailingInterruptProductionFactory()
    )

    states = _agent_run_states(worker)
    assert [status for status, _finished_at in states] == ["interrupted"]
    assert states[0][1] is not None


def test_attached_event_is_not_completed_before_consumption_is_guaranteed():
    gateway, queue, worker, _runtime, _sender, _vision, _resources = _system(
        debounce=0.08
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("attached", "m-attached", "text", text="看看这张图")
        )
        await worker.process(await queue.get())
        with worker.events.database.session() as session:
            assert session.get(StoredBotEvent, "attached").status == "processing"
        await image_task
        with worker.events.database.session() as session:
            assert session.get(StoredBotEvent, "attached").status == "completed"

    asyncio.run(scenario())


def test_text_arriving_during_agent_run_becomes_image_followup():
    class BlockingRuntime(Runtime):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_message(self, ctx, turn_input, **_kwargs):
            self.calls.append((ctx, turn_input))
            if len(self.calls) == 1:
                self.started.set()
                await self.release.wait()
            return RuntimeResponse(text=f"answer:{turn_input.text or 'image-only'}")

    runtime = BlockingRuntime()
    gateway, queue, worker, _, sender, _, _ = _system(
        runtime=runtime, debounce=0, association=1
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        await runtime.started.wait()
        assert gateway.accept_payload(
            _payload("late", "m-late", "text", text="刚才图里的报错怎么修？")
        )
        await worker.process(await queue.get())
        assert len(runtime.calls) == 1
        runtime.release.set()
        await image_task

    asyncio.run(scenario())
    assert [call[1].text for call in runtime.calls] == [
        "",
        "刚才图里的报错怎么修？",
    ]
    assert len(sender.texts) == 2


def test_text_arriving_during_strict_extractor_is_not_lost():
    started = asyncio.Event()
    release = asyncio.Event()

    class Drafts:
        def get(self, draft_id):
            assert draft_id == "draft-1"
            return {
                "id": draft_id,
                "structured_result": {"courses": [{"course_name": "高等数学"}]},
                "items": [{"course_name": "高等数学"}],
            }

    imports = SimpleNamespace(drafts=Drafts())
    gateway, queue, worker, runtime, sender, vision, _ = _system(
        schedule_imports=imports, debounce=0.2, association=1
    )

    async def strict(event, _participant_id, **_kwargs):
        started.set()
        await release.wait()
        await worker._deliver(event, "strict-preview")
        return ScheduleImageOutcome(
            "draft_created", {"id": "draft-1"}, "course_schedule"
        )

    worker._handle_schedule_image = strict

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("intent", "m-intent", "text", text="导入这张课程表到日历")
        )
        await worker.process(await queue.get())
        await started.wait()
        assert gateway.accept_payload(
            _payload("late", "m-late", "text", text="那周四呢？")
        )
        await worker.process(await queue.get())
        release.set()
        await image_task

    asyncio.run(scenario())
    assert vision.calls == []
    assert [call[1].text for call in runtime.calls] == ["那周四呢？"]
    assert runtime.calls[0][1].trusted_image_context["draft_id"] == "draft-1"
    assert runtime.calls[0][1].trusted_image_context["schedule"]["courses"][0][
        "course_name"
    ] == "高等数学"
    assert sender.texts == ["strict-preview", "answer:那周四呢？"]


def test_strict_failure_does_not_create_false_recent_context():
    gateway, queue, worker, _runtime, _sender, _vision, _ = _system(
        debounce=0.2
    )

    async def failed(event, _participant_id, **_kwargs):
        await worker._deliver(event, "strict-failed")
        return ScheduleImageOutcome("failed", None, "other")

    worker._handle_schedule_image = failed

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("intent", "m-intent", "text", text="导入这张课程表到日历")
        )
        await worker.process(await queue.get())
        await image_task
        person = worker.identity.resolve("app", "open")
        assert await worker.multimodal_turns.recent_context(
            person.id, "chat"
        ) is None

    asyncio.run(scenario())


def test_strict_not_course_does_not_cache_course_schedule_kind():
    gateway, queue, worker, _runtime, _sender, _vision, _ = _system(
        debounce=0.2
    )

    async def not_course(event, _participant_id, **_kwargs):
        await worker._deliver(event, "not-course")
        return ScheduleImageOutcome("not_course_schedule", None, "other")

    worker._handle_schedule_image = not_course

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("intent", "m-intent", "text", text="导入这张课程表到日历")
        )
        await worker.process(await queue.get())
        await image_task
        person = worker.identity.resolve("app", "open")
        assert await worker.multimodal_turns.recent_context(person.id, "chat") is None

    asyncio.run(scenario())


def test_generic_false_positive_course_schedule_falls_back_to_normal_image_agent():
    class StrictVision:
        model = "strict-vision"

        async def parse(self, _data, _mime):
            return ScheduleVisionResult.from_dict({
                "document_type": "not_course_schedule",
                "semester_label": None,
                "institution": None,
                "courses": [],
                "missing_context": [],
                "warnings": [],
            })

    class Drafts:
        def get_by_source(self, _participant_id, _message_id):
            return None

    gateway, queue, worker, runtime, sender, vision, resources = _system(
        vision=Vision(kind="course_schedule"),
        schedule_vision=StrictVision(),
        schedule_imports=SimpleNamespace(drafts=Drafts()),
        debounce=0.2,
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload(
                "intent",
                "m-intent",
                "text",
                text="把这个讲座添加到日历",
            )
        )
        await worker.process(await queue.get())
        await image_task

    asyncio.run(scenario())
    assert vision.calls == ["把这个讲座添加到日历"]
    assert len(resources.calls) == 1
    assert len(runtime.calls) == 1
    ctx, turn_input = runtime.calls[0]
    assert ctx.calendar_mutation_policy == "calendar_create_only"
    assert ctx.allows_calendar_mutation("create") is True
    assert ctx.allows_calendar_mutation("delete") is False
    assert turn_input.trusted_image_context == {
        "image_kind": "other",
        "summary": "图片摘要",
        "visible_text": "可见文字",
        "warnings": [],
    }
    assert sender.texts == ["answer:把这个讲座添加到日历"]


def test_strict_success_recent_followup_uses_authoritative_draft():
    class Drafts:
        def get(self, draft_id):
            assert draft_id == "authoritative-draft"
            return {
                "id": draft_id,
                "structured_result": {
                    "courses": [{
                        "course_name": "Repository 中的高等数学",
                        "weekday": 3,
                    }]
                },
                "items": [{"course_name": "Repository 中的高等数学"}],
            }

    imports = SimpleNamespace(drafts=Drafts())
    gateway, queue, worker, runtime, _sender, _vision, _resources = _system(
        schedule_imports=imports, debounce=0, recent=2
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_event = await queue.get()
        person = worker.identity.resolve("app", "open")
        turn = (
            await worker.multimodal_turns.open_image(
                person.id, image_event.chat_id, image_event
            )
        ).turn
        await worker.multimodal_turns.wait_for_debounce(turn)
        await worker.multimodal_turns.complete(
            turn,
            image_message_id=image_event.message_id,
            image_key=image_event.image_key,
            image_kind="course_schedule",
            summary={
                "route": "strict_schedule_fast_path",
                "draft_id": "authoritative-draft",
                "stale_summary": "不得用于回答",
            },
        )
        assert gateway.accept_payload(
            _payload("followup", "m-followup", "text", text="那周四呢？")
        )
        await worker.process(await queue.get())

    asyncio.run(scenario())
    trusted = runtime.calls[0][1].trusted_image_context
    assert trusted["draft_id"] == "authoritative-draft"
    assert "stale_summary" not in trusted
    assert trusted["schedule"]["courses"][0]["course_name"] == (
        "Repository 中的高等数学"
    )


def test_stop_after_two_consecutive_images_cancels_both_turns():
    vision = Vision(delay=1)
    gateway, queue, worker, runtime, sender, _, _ = _system(
        vision=vision, debounce=0, association=1
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image-1", "m-image-1", "image"))
        first = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(200):
            if vision.calls:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(_payload("image-2", "m-image-2", "image"))
        second = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(200):
            active = sum(len(value) for value in worker._active_multimodal_tasks.values())
            if active == 2:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(_payload("stop", "m-stop", "text", text="/stop"))
        await worker.process(await queue.get())
        await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(scenario())
    assert runtime.calls == []
    assert sender.texts == ["已请求停止当前处理。"]
    assert worker._active_multimodal_tasks == {}


def test_schedule_import_negation_precedes_keyword_fast_path():
    for text in (
        "先别导入日历，我只想问这个课程表周三有什么课",
        "不要添加到日历",
        "不用同步这个课表",
        "暂时不导入课程表",
        "我只想看看这个课程表",
        "我不想把课程表添加到日历",
    ):
        assert not is_strong_schedule_import_intent(
            text, image_kind="course_schedule"
        )


def test_schedule_import_how_to_question_is_not_fast_path():
    assert not is_strong_schedule_import_intent(
        "这个课程表怎么导入到日历？",
        image_kind="course_schedule",
    )


def test_add_note_to_schedule_is_not_calendar_import():
    assert not is_strong_schedule_import_intent(
        "帮我在这张课程表里添加备注",
        image_kind="course_schedule",
    )


def test_schedule_import_explanation_is_not_fast_path():
    assert not is_strong_schedule_import_intent(
        "课程表添加说明应该写什么？",
        image_kind="course_schedule",
    )


def test_course_word_alone_is_not_strong_schedule_noun():
    assert not is_strong_schedule_import_intent("把这个课程添加到日历")
    assert is_direct_image_calendar_request("把这个课程添加到日历")
    assert is_strong_schedule_import_intent("把这个课程表添加到日历")
    assert is_strong_schedule_import_intent(
        "把这些课程添加到日历", image_kind="course_schedule"
    )


def test_arbitrary_recent_image_does_not_capture_schedule_question():
    gateway, queue, worker, runtime, _sender, _vision, _resources = _system(
        vision=Vision(kind="photo"), debounce=0, association=1, recent=5
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(
            _payload("question", "m-question", "text", text="周三有什么课？")
        )
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(runtime.calls) == 2
    assert runtime.calls[1][1].trusted_image_context is None
    assert runtime.calls[1][0].calendar_mutation_policy == "normal"


def test_ordinary_image_direct_calendar_request_has_explicit_policy():
    gateway, queue, worker, runtime, _sender, _vision, _resources = _system(
        vision=Vision(kind="photo"), debounce=0.2
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload(
                "intent",
                "m-intent",
                "text",
                text="把这个讲座添加到日历",
            )
        )
        await worker.process(await queue.get())
        await image_task

    asyncio.run(scenario())
    assert runtime.calls[0][0].calendar_mutation_policy == (
        "calendar_create_only"
    )
    assert runtime.calls[0][0].calendar_mutation_allowed is True


def test_course_schedule_qa_uses_strict_parser_and_default_times_without_draft():
    class StrictVision:
        model = "strict-vision"

        def __init__(self):
            self.calls = 0

        async def parse(self, _data, _mime):
            self.calls += 1
            return _strict_schedule_result()

    class NoDrafts:
        def create_draft(self, *_args, **_kwargs):
            raise AssertionError("read-only schedule QA must not create a draft")

    strict = StrictVision()
    generic = Vision(failure=True)
    imports = SimpleNamespace(drafts=NoDrafts())
    gateway, queue, worker, runtime, _sender, _, resources = _system(
        vision=generic,
        schedule_vision=strict,
        schedule_imports=imports,
        debounce=0.2,
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("question", "m-question", "text", text="周三几点上课？")
        )
        await worker.process(await queue.get())
        await image_task

    asyncio.run(scenario())
    assert strict.calls == 1
    assert generic.calls == []
    assert len(resources.calls) == 1
    assert len(runtime.calls) == 1
    ctx, turn_input = runtime.calls[0]
    course = turn_input.trusted_image_context["schedule"]["courses"][0]
    assert (course["start_time"], course["end_time"]) == ("14:00", "15:40")
    assert ctx.calendar_mutation_policy == "course_schedule_strict_only"
    assert ctx.calendar_mutation_allowed is False


def test_read_only_schedule_context_never_reports_missing_time_after_backend_resolution():
    class StrictVision:
        model = "strict-vision"

        async def parse(self, _data, _mime):
            payload = _strict_schedule_result().to_dict()
            payload["missing_context"] = [
                "actual_time",
                "period_time_mapping",
            ]
            return ScheduleVisionResult.from_dict(payload)

    gateway, queue, worker, runtime, _sender, _, _resources = _system(
        vision=Vision(failure=True),
        schedule_vision=StrictVision(),
        schedule_imports=SimpleNamespace(drafts=object()),
        debounce=0.2,
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        image_task = asyncio.create_task(worker.process(await queue.get()))
        for _ in range(100):
            if worker._active_multimodal_tasks:
                break
            await asyncio.sleep(0.001)
        assert gateway.accept_payload(
            _payload("question", "m-question", "text", text="周三几点上课？")
        )
        await worker.process(await queue.get())
        await image_task

    asyncio.run(scenario())
    schedule = runtime.calls[0][1].trusted_image_context["schedule"]
    assert schedule["missing_context"] == []


def test_recent_course_schedule_qa_upgrades_generic_context_to_strict_parser():
    class StrictVision:
        model = "strict-vision"

        def __init__(self):
            self.calls = 0

        async def parse(self, _data, _mime):
            self.calls += 1
            return _strict_schedule_result()

    strict = StrictVision()
    generic = Vision(kind="course_schedule")
    gateway, queue, worker, runtime, _sender, _, resources = _system(
        vision=generic,
        schedule_vision=strict,
        debounce=0,
        association=0.01,
        recent=2,
    )

    async def scenario():
        assert gateway.accept_payload(_payload("image", "m-image", "image"))
        await worker.process(await queue.get())
        await asyncio.sleep(0.02)
        assert gateway.accept_payload(
            _payload("question", "m-question", "text", text="周三几点上课？")
        )
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert generic.calls == [""]
    assert strict.calls == 1
    assert len(resources.calls) == 2
    assert len(runtime.calls) == 2
    followup_context = runtime.calls[1][1].trusted_image_context
    assert followup_context["route"] == "strict_schedule_read_only"
    course = followup_context["schedule"]["courses"][0]
    assert (course["start_time"], course["end_time"]) == ("14:00", "15:40")
    assert runtime.calls[1][0].calendar_mutation_allowed is False
