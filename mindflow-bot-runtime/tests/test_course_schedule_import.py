import asyncio
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.contracts.course_schedule import (
    ScheduleVisionResult,
    ScheduleVisionValidationError,
)
from app.integrations.feishu.gateway import FeishuEventParser, InvalidBotEvent
from app.integrations.feishu.cards import course_schedule_preview_card
from app.integrations.feishu.message_resources import (
    FeishuMessageResourceDownloader,
    MessageResourceError,
    MessageResourceTooLarge,
    UnsupportedImageFormat,
)
from app.repositories_course_schedule import CourseScheduleImportRepository
from app.agent.skill_loader import SkillLoader
from app.identity.service import IdentityService
from app.integrations.feishu.gateway import FeishuGateway
from app.presentation.user_capabilities import help_text, onboarding_text
from app.repositories import (
    AgentRunRepository, BindingRepository, BotEventRepository, ParticipantRepository,
)
from app.worker import BotWorker
from app.services.course_schedule_import import (
    CourseScheduleImportService,
    normalize_import_item,
)
from app.services.course_schedule_vision import (
    CourseScheduleVisionError,
    CourseScheduleVisionService,
)
from helpers import memory_database, participant, skill_path


def vision_payload(*, odd_even="all", explicit_weeks=None, actual_times=True):
    return {
        "document_type": "course_schedule",
        "semester_label": "2026-2027-1",
        "institution": None,
        "courses": [{
            "course_name": "高等数学A",
            "weekday": 1,
            "period_start": 1,
            "period_end": 2,
            "start_time": "08:00" if actual_times else None,
            "end_time": "09:35" if actual_times else None,
            "location": "A101",
            "teacher": None,
            "week_rule": {
                "start_week": 1,
                "end_week": 16,
                "odd_even": odd_even,
                "explicit_weeks": explicit_weeks,
            },
            "uncertain_fields": [],
        }],
        "missing_context": ["semester_start_date"] + (
            [] if actual_times else ["period_time_mapping"]
        ),
        "warnings": [],
    }


def image_event_payload(message_type="image"):
    content = {"image_key": "img_v2_opaque"} if message_type == "image" else {"file_key": "f"}
    return {
        "header": {"event_id": "evt-image"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou"}},
            "message": {
                "message_id": "om-image", "chat_id": "oc", "chat_type": "p2p",
                "message_type": message_type, "content": json.dumps(content),
                "create_time": "1786200000000",
            },
        },
    }


def test_channel_adapter_accepts_image_and_ipc_round_trips():
    event = FeishuEventParser("app").parse(image_event_payload())
    assert event.message_type == "image"
    assert event.image_key == "img_v2_opaque"
    assert event.text == ""
    from app.integrations.feishu.gateway import BotEvent

    assert BotEvent.from_ipc_payload(event.to_ipc_payload()) == event


def test_unsupported_message_type_rejected():
    with pytest.raises(InvalidBotEvent, match="unsupported"):
        FeishuEventParser("app").parse(image_event_payload("file"))


def test_image_resource_limits_magic_and_timeout():
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 8
    valid = FeishuMessageResourceDownloader(object(), download=lambda *_: png, max_bytes=32)
    assert asyncio.run(valid.download_image("m", "k")).mime_type == "image/png"
    too_large = FeishuMessageResourceDownloader(
        object(), download=lambda *_: png, max_bytes=4
    )
    with pytest.raises(MessageResourceTooLarge):
        asyncio.run(too_large.download_image("m", "k"))
    invalid = FeishuMessageResourceDownloader(
        object(), download=lambda *_: b"not-an-image", max_bytes=32
    )
    with pytest.raises(UnsupportedImageFormat):
        asyncio.run(invalid.download_image("m", "k"))

    def slow(*_):
        import time
        time.sleep(0.05)
        return png

    timeout = FeishuMessageResourceDownloader(
        object(), download=slow, timeout_seconds=0.001
    )
    with pytest.raises(MessageResourceError, match="timed out"):
        asyncio.run(timeout.download_image("m", "k"))


def test_vision_valid_json_and_malformed_json_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "vision-model"
        assert body["temperature"] == 0
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(vision_payload())}}]
        })

    service = CourseScheduleVisionService(
        "https://vision.invalid/chat", "secret", "vision-model", enabled=True,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(service.parse(b"\x89PNG\r\n\x1a\n", "image/png"))
    assert result.courses[0].start_time == "08:00"

    malformed = CourseScheduleVisionService(
        "https://vision.invalid/chat", "secret", "vision-model", enabled=True,
        transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"choices": [{"message": {"content": "not json"}}]}
        )),
    )
    with pytest.raises(CourseScheduleVisionError):
        asyncio.run(malformed.parse(b"x", "image/png"))


def test_vision_schema_never_accepts_guessed_or_partial_times():
    payload = vision_payload()
    payload["courses"][0]["end_time"] = None
    with pytest.raises(ScheduleVisionValidationError, match="actual time range"):
        ScheduleVisionResult.from_dict(payload)


def _draft(database, person_id, *, odd_even="all", explicit_weeks=None):
    repo = CourseScheduleImportRepository(database)
    result = ScheduleVisionResult.from_dict(
        vision_payload(odd_even=odd_even, explicit_weeks=explicit_weeks)
    )
    return repo, repo.create_draft(
        person_id,
        source_message_id="om-source",
        source_image_hash="a" * 64,
        vision_model="vision-model",
        result=result,
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )


def test_draft_persists_and_calendar_never_writes_before_confirm():
    database = memory_database()
    person = participant(database, "P001")
    repo, draft = _draft(database, person.id)
    assert draft["status"] == "pending_confirmation"
    restarted_repo = CourseScheduleImportRepository(database)
    assert restarted_repo.get(draft["id"])["items"][0]["status"] == "pending"


def test_weekly_odd_even_and_explicit_week_normalization():
    database = memory_database()
    person = participant(database, "P001")
    _repo, odd = _draft(database, person.id, odd_even="odd")
    write = normalize_import_item(odd, odd["items"][0], timezone=ZoneInfo("Asia/Shanghai"))[0]
    assert "INTERVAL=2" in write.recurrence
    assert "COUNT=8" in write.recurrence
    assert write.start_time.date() == date(2026, 9, 7)

    database_even = memory_database()
    person_even = participant(database_even, "P008")
    _repo, even = _draft(database_even, person_even.id, odd_even="even")
    even_write = normalize_import_item(
        even, even["items"][0], timezone=ZoneInfo("Asia/Shanghai")
    )[0]
    assert "INTERVAL=2" in even_write.recurrence
    assert even_write.start_time.date() == date(2026, 9, 14)

    database2 = memory_database()
    person2 = participant(database2, "P002")
    _repo, explicit = _draft(database2, person2.id, explicit_weeks=[1, 2, 4, 7, 10])
    writes = normalize_import_item(
        explicit, explicit["items"][0], timezone=ZoneInfo("Asia/Shanghai")
    )
    assert len(writes) == 5
    assert all(write.recurrence is None for write in writes)


class Tokens:
    def status(self, _participant_id):
        return {"connected": True, "scopes": ["calendar:calendar.event:create"]}


class Calendar:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def create_event(self, _participant_id, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("provider failed")
        return {"id": f"event-{len(self.calls)}"}


def test_confirm_owner_idempotency_cancel_and_partial_retry():
    database = memory_database()
    owner = participant(database, "P001")
    other = participant(database, "P002")
    repo, draft = _draft(database, owner.id)
    calendar = Calendar()
    service = CourseScheduleImportService(repo, calendar, Tokens())
    with pytest.raises(PermissionError):
        asyncio.run(service.confirm(other.id, draft["id"]))
    result = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert result["succeeded"] == 1
    assert len(calendar.calls) == 1
    repeated = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert "已经添加过" in repeated["reply_text"]
    assert len(calendar.calls) == 1

    database2 = memory_database()
    owner2 = participant(database2, "P003")
    repo2, draft2 = _draft(database2, owner2.id)
    failing = Calendar(fail=True)
    service2 = CourseScheduleImportService(repo2, failing, Tokens())
    partial = asyncio.run(service2.confirm(owner2.id, draft2["id"]))
    assert partial["failed"] == 1
    failing.fail = False
    retried = asyncio.run(service2.confirm(owner2.id, draft2["id"]))
    assert retried["succeeded"] == 1
    assert len(failing.calls) == 2

    database3 = memory_database()
    owner3 = participant(database3, "P004")
    repo3, draft3 = _draft(database3, owner3.id)
    cancelled = service2.__class__(repo3, Calendar(), Tokens()).cancel(owner3.id, draft3["id"])
    assert cancelled["status"] == "cancelled"


def test_confirm_without_calendar_keeps_draft_and_writes_nothing():
    database = memory_database()
    owner = participant(database, "P001")
    repo, draft = _draft(database, owner.id)
    calendar = Calendar()

    class Disconnected:
        def status(self, _participant_id):
            return {"connected": False, "scopes": []}

    service = CourseScheduleImportService(repo, calendar, Disconnected())
    result = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert result["error"] == "calendar_not_connected"
    assert calendar.calls == []
    assert repo.get(draft["id"])["status"] == "pending_confirmation"


def test_bind_and_help_use_stable_copy_without_agent():
    database = memory_database()
    person = ParticipantRepository(database).create("P009")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue()
    gateway = FeishuGateway("app", "secret", identity, events, queue)

    class Runtime:
        def __init__(self):
            self.calls = 0

        async def handle_message(self, *_args, **_kwargs):
            self.calls += 1
            return "unexpected"

    class Sender:
        def __init__(self):
            self.sent = []

        def send_text(self, _chat_id, text, **_kwargs):
            self.sent.append(text)
            return f"om-{len(self.sent)}"

    runtime = Runtime()
    sender = Sender()

    class BlockedVision:
        def __init__(self):
            self.calls = 0

        async def parse(self, *_args):
            self.calls += 1
            raise AssertionError("Vision must not run without consent")

    vision = BlockedVision()
    worker = BotWorker(
        queue, identity, events, AgentRunRepository(database),
        SkillLoader(skill_path()), runtime, sender, model="fake",
        schedule_vision=vision,
    )

    def payload(event_id, message_id, text):
        return {
            "header": {"event_id": event_id},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou"}},
                "message": {
                    "message_id": message_id, "chat_id": "oc", "chat_type": "p2p",
                    "message_type": "text", "content": json.dumps({"text": text}),
                },
            },
        }

    async def scenario():
        assert gateway.accept_payload(payload("bind", "m-bind", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(payload("help", "m-help", "你能做什么？"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(image_event_payload())
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert sender.sent == [
        onboarding_text("P009"),
        help_text(),
        "暂时不能帮你识别这张课程表图片，因为还没有记录外部模型处理授权。请先联系研究者。",
    ]
    assert runtime.calls == 0
    assert vision.calls == 0


def test_missing_context_card_has_no_confirm_and_context_can_be_completed():
    database = memory_database()
    person = participant(database, "P001")
    repo = CourseScheduleImportRepository(database)
    result = ScheduleVisionResult.from_dict(vision_payload(actual_times=False))
    draft = repo.create_draft(
        person.id,
        source_message_id="om-context",
        source_image_hash="b" * 64,
        vision_model="vision-model",
        result=result,
        timezone_name="Asia/Shanghai",
    )
    assert draft["status"] == "pending_context"
    assert "course_schedule_import_confirm" not in json.dumps(
        course_schedule_preview_card(draft), ensure_ascii=False
    )
    draft = repo.set_semester_start_date(
        person.id, draft["id"], date(2026, 9, 7)
    )
    draft = repo.set_period_time_mapping(
        person.id,
        draft["id"],
        {(1, 2): (datetime.strptime("08:00", "%H:%M").time(),
                  datetime.strptime("09:35", "%H:%M").time())},
    )
    assert draft["status"] == "pending_confirmation"
    card_json = json.dumps(course_schedule_preview_card(draft), ensure_ascii=False)
    assert "course_schedule_import_confirm" in card_json
    assert '"courses"' not in card_json


def test_image_workflow_does_not_write_before_card_confirmation():
    database = memory_database()
    person = participant(database, "P001")
    identity = IdentityService(database, BindingRepository(database))
    code, _ = identity.create_invite(person.id)
    events = BotEventRepository(database)
    queue = asyncio.Queue()
    gateway = FeishuGateway("app", "secret", identity, events, queue)
    drafts = CourseScheduleImportRepository(database)
    calendar = Calendar()
    imports = CourseScheduleImportService(drafts, calendar, Tokens())

    class Runtime:
        async def handle_message(self, *_args, **_kwargs):
            raise AssertionError("image workflow must not call the Agent")

    class Sender:
        def __init__(self):
            self.texts = []
            self.cards = []

        def send_text(self, _chat_id, text, **_kwargs):
            self.texts.append(text)
            return "om-text"

        def send_card(self, _chat_id, card, **_kwargs):
            self.cards.append(card)
            return "om-card"

    class Resources:
        async def download_image(self, _message_id, _image_key):
            return SimpleNamespace(data=b"\x89PNG\r\n\x1a\n", mime_type="image/png")

    class Vision:
        model = "vision-model"

        async def parse(self, _data, _mime):
            return ScheduleVisionResult.from_dict(vision_payload())

    sender = Sender()
    worker = BotWorker(
        queue, identity, events, AgentRunRepository(database), SkillLoader(skill_path()),
        Runtime(), sender, model="fake", schedule_vision=Vision(),
        schedule_imports=imports, message_resources=Resources(),
    )

    def text_payload(event_id, message_id, text):
        return {
            "header": {"event_id": event_id},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou"}},
                "message": {
                    "message_id": message_id, "chat_id": "oc", "chat_type": "p2p",
                    "message_type": "text", "content": json.dumps({"text": text}),
                },
            },
        }

    async def scenario():
        assert gateway.accept_payload(text_payload("bind", "m-bind", f"/bind {code}"))
        await worker.process(await queue.get())
        assert gateway.accept_payload(image_event_payload())
        await worker.process(await queue.get())
        assert calendar.calls == []
        assert gateway.accept_payload(text_payload("context", "m-context", "2026-09-07"))
        await worker.process(await queue.get())

    asyncio.run(scenario())
    assert len(sender.cards) == 2
    assert "course_schedule_import_confirm" in json.dumps(sender.cards[-1])
    assert calendar.calls == []


def test_expired_draft_is_rejected_and_persisted_as_expired():
    database = memory_database()
    owner = participant(database, "P001")
    repo = CourseScheduleImportRepository(database)
    draft = repo.create_draft(
        owner.id,
        source_message_id="om-expired",
        source_image_hash="c" * 64,
        vision_model="vision-model",
        result=ScheduleVisionResult.from_dict(vision_payload()),
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
        ttl_minutes=1,
        now=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="expired"):
        asyncio.run(
            CourseScheduleImportService(repo, Calendar(), Tokens()).confirm(
                owner.id, draft["id"]
            )
        )
    assert repo.get(draft["id"])["status"] == "expired"


def test_batch_calendar_mutation_invalidates_and_enqueues_forecast_once():
    database = memory_database()
    owner = participant(database, "P001")
    repo, draft = _draft(database, owner.id)

    class Forecasts:
        def __init__(self):
            self.calls = []

        def invalidate_for_calendar_mutation_dates(
            self, warnings, participant_id, dates, *, reason
        ):
            self.calls.append((warnings, participant_id, set(dates), reason))

    class Refresh:
        reconciliations = None

        def __init__(self):
            self.calls = []

        def enqueue(self, participant_id, dates, **kwargs):
            self.calls.append((participant_id, dict(dates), kwargs))
            return True

    forecasts = Forecasts()
    refresh = Refresh()
    coordinator = SimpleNamespace(warnings=object(), dependency_refresh=None)
    service = CourseScheduleImportService(
        repo,
        Calendar(),
        Tokens(),
        forecast_coordinator=coordinator,
        forecast_snapshots=forecasts,
        mutation_refresh=refresh,
    )
    result = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert result["succeeded"] == 1
    assert len(forecasts.calls) == 1
    assert len(refresh.calls) == 1
    assert forecasts.calls[0][3] == "course_schedule_import"
