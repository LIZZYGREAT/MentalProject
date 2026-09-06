import asyncio
import json
from types import SimpleNamespace

from app.agent.skill_loader import SkillLoader
from app.contracts.course_schedule import ScheduleVisionResult
from app.contracts.generic_image_context import GenericImageContext
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.models import BotEvent as StoredBotEvent
from app.presentation.contracts import RuntimeResponse
from app.repositories import AgentRunRepository, BindingRepository, BotEventRepository
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


def test_explicit_schedule_import_is_fast_path_without_generic_vision_or_agent():
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
