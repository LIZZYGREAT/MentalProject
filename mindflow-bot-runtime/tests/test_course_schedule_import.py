import asyncio
from datetime import date, datetime, time, timedelta, timezone
import json
import threading
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

import httpx
import pytest
import app.services.course_schedule_vision as course_schedule_vision_module

from app.contracts.course_schedule import (
    ScheduleVisionResult,
    ScheduleVisionValidationError,
)
from app.integrations.feishu.gateway import BotEvent, FeishuEventParser, InvalidBotEvent
from app.integrations.feishu.cards import (
    course_schedule_preview_card,
    course_schedule_result_card,
)
from app.integrations.feishu.message_resources import (
    FeishuMessageResourceDownloader,
    MessageResourceError,
    MessageResourceTooLarge,
    UnsupportedImageFormat,
)
from app.models import CourseScheduleImport
from app.repositories_course_schedule import (
    CourseScheduleImportRepository,
    UnfillableScheduleContextError,
    derive_required_context,
)
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
from app.domain.course_schedule_recurrence import (
    EXPAND_ALL_OCCURRENCES,
    PRESERVE_SCHEDULE_PATTERN,
    CalendarWriteKind,
    plan_course_writes,
)
from app.domain.course_schedule_periods import (
    DEFAULT_SCHOOL_PERIODS,
    resolve_period_time,
)
from app.services.course_schedule_vision import (
    CourseScheduleVisionError,
    CourseScheduleVisionService,
)
from app.services.card_action_service import CardActionService
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
    card = course_schedule_preview_card(draft)
    payload = json.dumps(card, ensure_ascii=False)
    assert "按课表周期规则添加" in payload
    assert "全部拆成单次日程" in payload
    assert "取消" in payload
    assert PRESERVE_SCHEDULE_PATTERN in payload
    assert EXPAND_ALL_OCCURRENCES in payload
    visible_text = "\n".join(
        element.get("content", "") for element in card["body"]["elements"]
    )
    assert "重复方式：每周重复" in visible_text
    assert not any(
        token in visible_text
        for token in ("RRULE", "FREQ", "recurrence_strategy", "planner_version")
    )


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


def test_course_recurrence_planner_classifies_preserve_patterns():
    timezone_name = ZoneInfo("Asia/Shanghai")

    database = memory_database()
    person = participant(database, "PLAN-WEEKLY")
    _repo, weekly = _draft(database, person.id)
    weekly_writes = plan_course_writes(
        weekly, weekly["items"][0], strategy=PRESERVE_SCHEDULE_PATTERN,
        timezone=timezone_name,
    )
    assert len(weekly_writes) == 1
    assert weekly_writes[0].write_kind == CalendarWriteKind.RECURRING
    assert "INTERVAL=1" in str(weekly_writes[0].recurrence)
    assert "COUNT=16" in str(weekly_writes[0].recurrence)
    assert len(weekly_writes[0].affected_dates) == 16

    one_week_payload = vision_payload(explicit_weeks=[6])
    one_week_result = ScheduleVisionResult.from_dict(one_week_payload)
    one_week_repo = CourseScheduleImportRepository(database)
    one_week = one_week_repo.create_draft(
        person.id, source_message_id="plan-one-week", source_image_hash="2" * 64,
        vision_model="vision-model", result=one_week_result,
        timezone_name="Asia/Shanghai", semester_start_date=date(2026, 9, 7),
    )
    one_week_writes = plan_course_writes(
        one_week, one_week["items"][0], strategy=PRESERVE_SCHEDULE_PATTERN,
        timezone=timezone_name,
    )
    assert len(one_week_writes) == 1
    assert one_week_writes[0].write_kind == CalendarWriteKind.SINGLE
    assert one_week_writes[0].recurrence is None


def test_explicit_periodic_and_irregular_course_weeks_are_deterministic():
    timezone_name = ZoneInfo("Asia/Shanghai")
    database = memory_database()
    person = participant(database, "PLAN-EXPLICIT")
    _repo, periodic = _draft(
        database, person.id, explicit_weeks=[1, 3, 5, 7]
    )
    periodic_writes = plan_course_writes(
        periodic, periodic["items"][0], strategy=PRESERVE_SCHEDULE_PATTERN,
        timezone=timezone_name,
    )
    assert len(periodic_writes) == 1
    assert periodic_writes[0].write_kind == CalendarWriteKind.RECURRING
    assert "INTERVAL=2" in str(periodic_writes[0].recurrence)
    assert "COUNT=4" in str(periodic_writes[0].recurrence)

    database2 = memory_database()
    person2 = participant(database2, "PLAN-IRREGULAR")
    _repo, irregular = _draft(
        database2, person2.id, explicit_weeks=[1, 2, 4, 7, 10]
    )
    irregular_writes = plan_course_writes(
        irregular, irregular["items"][0], strategy=PRESERVE_SCHEDULE_PATTERN,
        timezone=timezone_name,
    )
    assert len(irregular_writes) == 5
    assert all(write.write_kind == CalendarWriteKind.SINGLE for write in irregular_writes)
    assert [write.affected_dates[0] for write in irregular_writes] == [
        date(2026, 9, 7) + timedelta(weeks=week - 1)
        for week in [1, 2, 4, 7, 10]
    ]


@pytest.mark.parametrize(
    ("weeks", "interval", "visible_label"),
    [
        ([1, 2, 3], 1, "每周重复"),
        ([1, 3, 5], 2, "每两周重复"),
        ([1, 4, 7], 3, "每3周重复"),
    ],
)
def test_preview_uses_exact_planned_week_interval(weeks, interval, visible_label):
    database = memory_database()
    person = participant(database, f"PLAN-INTERVAL-{interval}")
    _repo, draft = _draft(database, person.id, explicit_weeks=weeks)
    writes = plan_course_writes(
        draft, draft["items"][0], strategy=PRESERVE_SCHEDULE_PATTERN,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    assert writes[0].recurrence_interval == interval
    assert f"INTERVAL={interval}" in str(writes[0].recurrence)
    card = course_schedule_preview_card(draft)
    visible_text = "\n".join(
        element.get("content", "") for element in card["body"]["elements"]
    )
    assert f"重复方式：{visible_label}" in visible_text
    if interval > 2:
        assert "重复方式：每周重复" not in visible_text


def test_expand_all_occurrences_creates_only_independent_events():
    database = memory_database()
    person = participant(database, "PLAN-EXPAND")
    _repo, draft = _draft(database, person.id)
    writes = plan_course_writes(
        draft, draft["items"][0], strategy=EXPAND_ALL_OCCURRENCES,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    assert len(writes) == 16
    assert all(write.write_kind == CalendarWriteKind.SINGLE for write in writes)
    assert all(write.recurrence is None for write in writes)
    assert len({write.occurrence_identity for write in writes}) == 16


class Tokens:
    def status(self, _participant_id):
        return {"connected": True, "scopes": ["calendar:calendar.event:create"]}


class Calendar:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def _create(self, _participant_id, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("provider failed")
        return {"id": f"event-{len(self.calls)}"}

    async def create_single_event(self, participant_id, **kwargs):
        kwargs["write_kind"] = "single"
        return await self._create(participant_id, **kwargs)

    async def create_recurring_event(self, participant_id, **kwargs):
        kwargs["write_kind"] = "recurring"
        return await self._create(participant_id, **kwargs)

    async def create_event(self, participant_id, **kwargs):
        if kwargs.get("recurrence"):
            return await self.create_recurring_event(participant_id, **kwargs)
        kwargs.pop("recurrence", None)
        return await self.create_single_event(participant_id, **kwargs)


def test_confirm_owner_idempotency_cancel_and_partial_retry():
    database = memory_database()
    owner = participant(database, "P001")
    other = participant(database, "P002")
    repo, draft = _draft(database, owner.id)
    calendar = Calendar()
    service = CourseScheduleImportService(repo, calendar, Tokens())
    with pytest.raises(PermissionError):
        asyncio.run(service.confirm(other.id, draft["id"]))
    result = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
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
    partial = asyncio.run(service2.confirm(
        owner2.id, draft2["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
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
    result = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
    assert result["error"] == "calendar_not_connected"
    assert calendar.calls == []
    assert repo.get(draft["id"])["status"] == "pending_confirmation"


def test_recurrence_strategy_is_durable_and_immutable_after_writes_begin():
    database = memory_database()
    owner = participant(database, "STRATEGY-LOCK")
    repo, draft = _draft(database, owner.id)
    selected = repo.set_recurrence_strategy(
        owner.id, draft["id"], PRESERVE_SCHEDULE_PATTERN
    )
    assert selected["recurrence_strategy"] == PRESERVE_SCHEDULE_PATTERN
    assert selected["recurrence_confirmed_at"] is not None
    repo.begin_confirmation(owner.id, draft["id"])
    with pytest.raises(ValueError, match="immutable"):
        repo.set_recurrence_strategy(
            owner.id, draft["id"], EXPAND_ALL_OCCURRENCES
        )


def test_calendar_write_cap_rejects_before_any_provider_or_item_mutation():
    database = memory_database()
    owner = participant(database, "WRITE-CAP")
    repo, draft = _draft(database, owner.id)
    calendar = Calendar()
    service = CourseScheduleImportService(
        repo, calendar, Tokens(), max_calendar_writes=15
    )
    result = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=EXPAND_ALL_OCCURRENCES
    ))
    persisted = repo.get(draft["id"])
    assert result["error"] == "calendar_write_limit_exceeded"
    assert result["planned_writes"] == 16
    assert calendar.calls == []
    assert persisted["status"] == "pending_confirmation"
    assert all(item["status"] == "pending" for item in persisted["items"])
    assert persisted["recurrence_strategy"] == EXPAND_ALL_OCCURRENCES
    card_payload = json.dumps(course_schedule_result_card(
        result["reply_text"], status=result["status"], import_id=result["import_id"],
        error=result["error"], recurrence_strategy=result["recurrence_strategy"],
    ), ensure_ascii=False)
    assert "改用按课表周期规则添加" in card_payload
    assert "course_schedule_import_cancel" in card_payload


def test_preserve_write_cap_has_cancel_recovery_without_strategy_misdirection():
    database = memory_database()
    owner = participant(database, "PRESERVE-CAP")
    repo, draft = _draft(
        database, owner.id, explicit_weeks=[1, 2, 4, 7, 10]
    )
    calendar = Calendar()
    service = CourseScheduleImportService(
        repo, calendar, Tokens(), max_calendar_writes=4
    )
    result = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
    persisted = repo.get(draft["id"])
    assert result["error"] == "calendar_write_limit_exceeded"
    assert result["planned_writes"] == 5
    assert "改用“按课表周期规则添加”" not in result["reply_text"]
    assert "取消后拆分课程表重新导入" in result["reply_text"]
    assert calendar.calls == []
    assert persisted["status"] == "pending_confirmation"
    assert persisted["recurrence_strategy"] == PRESERVE_SCHEDULE_PATTERN
    assert all(item["status"] == "pending" for item in persisted["items"])

    card_payload = json.dumps(course_schedule_result_card(
        result["reply_text"], status=result["status"], import_id=result["import_id"],
        error=result["error"], recurrence_strategy=result["recurrence_strategy"],
    ), ensure_ascii=False)
    assert "course_schedule_import_cancel" in card_payload
    assert "改用按课表周期规则添加" not in card_payload
    assert EXPAND_ALL_OCCURRENCES not in card_payload


def test_period_defaults_cover_one_to_fourteen_and_respect_priority():
    expected = {
        1: (time(8, 0), time(8, 45)),
        2: (time(8, 55), time(9, 40)),
        3: (time(10, 0), time(10, 45)),
        4: (time(10, 55), time(11, 40)),
        5: (time(12, 0), time(12, 45)),
        6: (time(12, 55), time(13, 40)),
        7: (time(14, 0), time(14, 45)),
        8: (time(14, 55), time(15, 40)),
        9: (time(16, 0), time(16, 45)),
        10: (time(16, 55), time(17, 40)),
        11: (time(18, 30), time(19, 15)),
        12: (time(19, 25), time(20, 10)),
        13: (time(20, 30), time(21, 15)),
        14: (time(21, 25), time(22, 10)),
    }
    assert DEFAULT_SCHOOL_PERIODS == expected
    for period, clocks in expected.items():
        assert resolve_period_time(period, period) == (*clocks, "default")
    assert resolve_period_time(1, 2) == (time(8), time(9, 40), "default")
    assert resolve_period_time(3, 4) == (time(10), time(11, 40), "default")
    assert resolve_period_time(7, 8) == (time(14), time(15, 40), "default")
    assert resolve_period_time(9, 10) == (time(16), time(17, 40), "default")
    assert resolve_period_time(11, 12) == (time(18, 30), time(20, 10), "default")
    assert resolve_period_time(13, 14) == (time(20, 30), time(22, 10), "default")

    user = {1: (time(7, 50), time(8, 35)), 2: (time(8, 45), time(9, 30))}
    ranges = {(1, 2): (time(7, 40), time(9, 20))}
    assert resolve_period_time(1, 2, period_mapping=user) == (
        time(7, 50), time(9, 30), "user"
    )
    assert resolve_period_time(
        1, 2, period_mapping=user, range_overrides=ranges
    ) == (time(7, 40), time(9, 20), "user")
    assert resolve_period_time(
        1, 2, actual_start=time(8, 10), actual_end=time(9, 50),
        period_mapping=user, range_overrides=ranges,
    ) == (time(8, 10), time(9, 50), "image")


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
    payload = vision_payload(actual_times=False)
    payload["courses"][0]["period_start"] = 15
    payload["courses"][0]["period_end"] = 16
    result = ScheduleVisionResult.from_dict(payload)
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
        {(15, 16): (datetime.strptime("08:00", "%H:%M").time(),
                    datetime.strptime("09:35", "%H:%M").time())},
    )
    assert draft["status"] == "pending_confirmation"
    card_json = json.dumps(course_schedule_preview_card(draft), ensure_ascii=False)
    assert "course_schedule_import_confirm" in card_json
    assert '"courses"' not in card_json


def test_sequential_single_period_overrides_are_merged_durably_across_restart():
    database = memory_database()
    owner = participant(database, "PERIOD-MERGE")
    repo, draft = _period_only_draft(database, owner.id, "period-merge")
    first = repo.set_period_time_mapping(
        owner.id, draft["id"], {1: (time(7, 50), time(8, 35))}
    )
    assert first["items"][0]["start_time"] == "07:50"
    assert first["items"][0]["end_time"] == "09:40"

    restarted = CourseScheduleImportRepository(database)
    second = restarted.set_period_time_mapping(
        owner.id, draft["id"], {2: (time(8, 45), time(9, 30))}
    )
    assert second["items"][0]["start_time"] == "07:50"
    assert second["items"][0]["end_time"] == "09:30"
    assert second["structured_result"]["_metadata"]["user_period_mapping"] == {
        "1": ["07:50", "08:35"],
        "2": ["08:45", "09:30"],
    }


def test_later_single_period_override_replaces_same_persisted_key():
    database = memory_database()
    owner = participant(database, "PERIOD-REPLACE")
    repo, draft = _period_only_draft(database, owner.id, "period-replace")
    repo.set_period_time_mapping(
        owner.id, draft["id"], {1: (time(7, 50), time(8, 35))}
    )
    replaced = repo.set_period_time_mapping(
        owner.id, draft["id"], {1: (time(8, 10), time(8, 55))}
    )
    assert replaced["items"][0]["start_time"] == "08:10"
    assert replaced["items"][0]["end_time"] == "09:40"
    assert replaced["structured_result"]["_metadata"]["user_period_mapping"] == {
        "1": ["08:10", "08:55"]
    }


def test_legacy_draft_image_time_is_never_overwritten_by_user_period_mapping():
    database = memory_database()
    owner = participant(database, "PERIOD-IMAGE-PRIORITY")
    repo, draft = _draft(database, owner.id)
    with database.session() as session:
        row = session.get(CourseScheduleImport, uuid.UUID(draft["id"]))
        structured = dict(row.structured_result)
        structured.pop("_metadata", None)
        row.structured_result = structured

    updated = repo.set_period_time_mapping(
        owner.id,
        draft["id"],
        {(1, 2): (time(7, 40), time(9, 20))},
    )
    assert updated["items"][0]["start_time"] == "08:00"
    assert updated["items"][0]["end_time"] == "09:35"
    assert updated["structured_result"]["_metadata"]["course_time_sources"] == [
        "image"
    ]


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


def test_pending_context_and_pending_confirmation_are_the_only_ttl_states():
    old = datetime.now(timezone.utc) - timedelta(minutes=2)

    database = memory_database()
    owner = participant(database, "TTL-CONTEXT")
    payload = vision_payload(actual_times=False)
    payload["courses"][0]["period_start"] = 15
    payload["courses"][0]["period_end"] = 16
    repo = CourseScheduleImportRepository(database)
    pending_context = repo.create_draft(
        owner.id, source_message_id="ttl-context", source_image_hash="3" * 64,
        vision_model="vision-model", result=ScheduleVisionResult.from_dict(payload),
        timezone_name="Asia/Shanghai", ttl_minutes=1, now=old,
    )
    with pytest.raises(ValueError, match="expired"):
        repo.validate_for_confirmation(owner.id, pending_context["id"])
    assert repo.get(pending_context["id"])["status"] == "expired"

    running_db = memory_database()
    running_owner = participant(running_db, "TTL-RUNNING")
    running_repo, running = _draft(running_db, running_owner.id)
    running_repo.set_recurrence_strategy(
        running_owner.id, running["id"], PRESERVE_SCHEDULE_PATTERN
    )
    started = running_repo.begin_confirmation(running_owner.id, running["id"])
    _expire_draft_ttl(running_db, running["id"])
    _expire_run_lease(running_db, running["id"])
    reclaimed = running_repo.begin_confirmation(running_owner.id, running["id"])
    assert started["status"] == "running"
    assert reclaimed["claimed"] is True
    assert reclaimed["status"] == "running"


def test_partial_failed_import_remains_retryable_after_original_ttl():
    database = memory_database()
    owner = participant(database, "TTL-PARTIAL")
    repo, draft = _draft(database, owner.id)
    calendar = Calendar(fail=True)
    service = CourseScheduleImportService(repo, calendar, Tokens())
    first = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
    assert first["status"] == "partial_failed"
    _expire_draft_ttl(database, draft["id"])
    calendar.fail = False
    retried = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert retried["status"] == "succeeded"


def test_sync_download_timeout_holds_real_concurrency_slot_until_thread_exits():
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 8
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def blocking_download(message_id, _image_key):
        if message_id == "first":
            first_entered.set()
            release_first.wait(timeout=2)
        else:
            second_entered.set()
        return png

    async def scenario():
        downloader = FeishuMessageResourceDownloader(
            object(), download=blocking_download, timeout_seconds=0.02,
            max_concurrency=1,
        )
        with pytest.raises(MessageResourceError, match="timed out"):
            await downloader.download_image("first", "image-a")
        assert first_entered.is_set()
        second = asyncio.create_task(
            downloader.download_image("second", "image-b")
        )
        await asyncio.sleep(0.03)
        assert not second_entered.is_set()
        release_first.set()
        result = await second
        assert result.data == png
        assert second_entered.is_set()

    asyncio.run(scenario())


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
    result = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
    assert result["succeeded"] == 1
    assert len(forecasts.calls) == 1
    assert len(refresh.calls) == 1
    assert forecasts.calls[0][3] == "course_schedule_import"


def test_stale_running_draft_can_be_reclaimed():
    database = memory_database()
    owner = participant(database, "P101")
    repo = CourseScheduleImportRepository(database, run_lease_seconds=60)
    _unused, draft = _draft_with_repo(repo, owner.id, "stale-draft")
    repo.set_recurrence_strategy(owner.id, draft["id"], PRESERVE_SCHEDULE_PATTERN)
    started = datetime.now(timezone.utc)
    first = repo.begin_confirmation(owner.id, draft["id"], now=started)
    assert first["claimed"] is True

    reclaimed = repo.begin_confirmation(
        owner.id, draft["id"], now=started + timedelta(seconds=61)
    )
    assert reclaimed["claimed"] is True
    assert reclaimed["run_claimed_at"] == (
        started + timedelta(seconds=61)
    ).isoformat()


def test_running_draft_with_live_lease_is_not_reclaimed():
    database = memory_database()
    owner = participant(database, "P102")
    repo = CourseScheduleImportRepository(database, run_lease_seconds=60)
    _unused, draft = _draft_with_repo(repo, owner.id, "live-draft")
    repo.set_recurrence_strategy(owner.id, draft["id"], PRESERVE_SCHEDULE_PATTERN)
    started = datetime.now(timezone.utc)
    repo.begin_confirmation(owner.id, draft["id"], now=started)

    repeated = repo.begin_confirmation(
        owner.id, draft["id"], now=started + timedelta(seconds=30)
    )
    assert repeated["claimed"] is False
    assert repeated["status"] == "running"


def test_stale_running_items_reset_to_pending():
    database = memory_database()
    owner = participant(database, "P103")
    repo = CourseScheduleImportRepository(database, run_lease_seconds=60)
    _unused, draft = _draft_with_repo(repo, owner.id, "stale-item")
    repo.set_recurrence_strategy(owner.id, draft["id"], PRESERVE_SCHEDULE_PATTERN)
    started = datetime.now(timezone.utc)
    running = repo.begin_confirmation(owner.id, draft["id"], now=started)
    assert repo.claim_item(
        draft["id"], running["items"][0]["id"], now=started
    )

    reclaimed = repo.begin_confirmation(
        owner.id, draft["id"], now=started + timedelta(seconds=61)
    )
    assert reclaimed["claimed"] is True
    assert reclaimed["items"][0]["status"] == "pending"


class IdempotentCalendar:
    def __init__(self):
        self.attempts = []
        self.events = {}

    async def _create(self, _participant_id, **kwargs):
        source = kwargs["source_message_id"]
        self.attempts.append(source)
        if source not in self.events:
            self.events[source] = {"id": f"event-{len(self.events) + 1}"}
        return self.events[source]

    async def create_single_event(self, participant_id, **kwargs):
        return await self._create(participant_id, **kwargs)

    async def create_recurring_event(self, participant_id, **kwargs):
        return await self._create(participant_id, **kwargs)

    async def create_event(self, participant_id, **kwargs):
        return await self._create(participant_id, **kwargs)


def test_restart_after_remote_create_does_not_duplicate_calendar_event():
    database = memory_database()
    owner = participant(database, "P104")
    repo, draft = _draft(database, owner.id)
    repo.set_recurrence_strategy(owner.id, draft["id"], PRESERVE_SCHEDULE_PATTERN)
    claimed = repo.begin_confirmation(owner.id, draft["id"])
    item = claimed["items"][0]
    assert repo.claim_item(draft["id"], item["id"])
    write = normalize_import_item(
        claimed, item, timezone=ZoneInfo("Asia/Shanghai")
    )[0]
    source_id = (
        f"schedule:{draft['id']}:{PRESERVE_SCHEDULE_PATTERN}:"
        f"{item['normalized_key']}:"
        f"{write.occurrence_identity}"
    )
    calendar = IdempotentCalendar()
    asyncio.run(calendar.create_event(
        owner.id,
        summary=write.summary,
        start_time=write.start_time,
        end_time=write.end_time,
        description=write.description,
        recurrence=write.recurrence,
        source_message_id=source_id,
    ))
    _expire_run_lease(database, draft["id"])

    restarted = CourseScheduleImportRepository(database)
    result = asyncio.run(
        CourseScheduleImportService(restarted, calendar, Tokens()).confirm(
            owner.id, draft["id"]
        )
    )
    assert result["status"] == "succeeded"
    assert len(calendar.events) == 1
    assert calendar.attempts == [source_id, source_id]


def test_partial_success_then_crash_can_resume_and_finalize():
    database = memory_database()
    owner = participant(database, "P105")
    repo, draft = _two_course_draft(database, owner.id, "partial-crash")
    repo.set_recurrence_strategy(owner.id, draft["id"], PRESERVE_SCHEDULE_PATTERN)
    claimed = repo.begin_confirmation(owner.id, draft["id"])
    first, second = claimed["items"]
    assert repo.claim_item(draft["id"], first["id"])
    repo.finish_item(draft["id"], first["id"], calendar_event_id="event-first")
    assert repo.claim_item(draft["id"], second["id"])
    _expire_run_lease(database, draft["id"])

    calendar = Calendar()
    result = asyncio.run(
        CourseScheduleImportService(
            CourseScheduleImportRepository(database), calendar, Tokens()
        ).confirm(owner.id, draft["id"])
    )
    assert result["status"] == "succeeded"
    assert result["succeeded"] == 2
    assert [call["summary"] for call in calendar.calls] == ["线性代数"]


def test_calendar_not_connected_card_keeps_same_confirm_action():
    database = memory_database()
    owner = participant(database, "P109")
    repo, draft = _draft(database, owner.id)

    class Disconnected:
        def status(self, _participant_id):
            return {"connected": False, "scopes": []}

    handler = CardActionService(
        object(),
        observation_refresh=object(),
        course_schedule_imports=CourseScheduleImportService(
            repo, Calendar(), Disconnected()
        ),
    )
    result = handler.handle(
        owner.id,
        message_id="card-message",
        action_value={
            "mindflow_action": "course_schedule_import_confirm",
            "version": "2",
            "import_id": draft["id"],
            "recurrence_strategy": PRESERVE_SCHEDULE_PATTERN,
        },
        form_value={},
    )
    payload = json.dumps(result["card"], ensure_ascii=False)
    assert "course_schedule_import_confirm" in payload
    assert "course_schedule_import_cancel" in payload
    assert draft["id"] in payload


def test_calendar_authorize_then_same_draft_confirm_succeeds():
    database = memory_database()
    owner = participant(database, "P106")
    repo, draft = _draft(database, owner.id)
    calendar = Calendar()

    class ToggleTokens:
        connected = False

        def status(self, _participant_id):
            return {
                "connected": self.connected,
                "scopes": ["calendar:calendar.event:create"] if self.connected else [],
            }

    tokens = ToggleTokens()
    service = CourseScheduleImportService(repo, calendar, tokens)
    first = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
    assert first["error"] == "calendar_not_connected"
    assert first["import_id"] == draft["id"]
    tokens.connected = True
    second = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert second["status"] == "succeeded"
    assert len(calendar.calls) == 1


def test_partial_failed_card_has_retry_failed_action():
    import_id = str(uuid.uuid4())
    card = course_schedule_result_card(
        "已添加 18 项，有 2 项没能添加。",
        status="partial_failed",
        import_id=import_id,
        recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "重试失败项" in payload
    assert "course_schedule_import_confirm" in payload
    assert "course_schedule_import_cancel" not in payload
    assert import_id in payload


def test_retry_failed_items_does_not_recreate_succeeded_items():
    database = memory_database()
    owner = participant(database, "P107")
    repo, draft = _two_course_draft(database, owner.id, "retry-failed")

    class FailSecondOnce(Calendar):
        failed = False

        async def _create(self, participant_id, **kwargs):
            self.calls.append(kwargs)
            if kwargs["summary"] == "线性代数" and not self.failed:
                self.failed = True
                raise RuntimeError("provider failed")
            return {"id": f"event-{len(self.calls)}"}

    calendar = FailSecondOnce()
    service = CourseScheduleImportService(repo, calendar, Tokens())
    first = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
    assert first["status"] == "partial_failed"
    second = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert second["status"] == "succeeded"
    summaries = [call["summary"] for call in calendar.calls]
    assert summaries.count("高等数学A") == 1
    assert summaries.count("线性代数") == 2


def test_mid_batch_calendar_authorization_loss_preserves_strategy_and_resumes():
    database = memory_database()
    owner = participant(database, "AUTH-LOSS")
    repo, draft = _two_course_draft(database, owner.id, "auth-loss")

    class LoseAuthorizationOnce(Calendar):
        denied = False

        async def _create(self, participant_id, **kwargs):
            self.calls.append(kwargs)
            if kwargs["summary"] == "线性代数" and not self.denied:
                self.denied = True
                raise PermissionError("calendar authorization expired")
            return {"id": f"event-{len(self.calls)}"}

    calendar = LoseAuthorizationOnce()
    service = CourseScheduleImportService(repo, calendar, Tokens())
    first = asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
    assert first["status"] == "partial_failed"
    assert first["error"] == "calendar_not_connected"
    assert first["recurrence_strategy"] == PRESERVE_SCHEDULE_PATTERN
    assert "重新完成 /calendar 授权" in first["reply_text"]
    assert "当前导入方式：按课表周期规则" in first["reply_text"]
    assert repo.get(draft["id"])["recurrence_strategy"] == PRESERVE_SCHEDULE_PATTERN

    second = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert second["status"] == "succeeded"
    summaries = [call["summary"] for call in calendar.calls]
    assert summaries.count("高等数学A") == 1
    assert summaries.count("线性代数") == 2
    assert all(
        f":{PRESERVE_SCHEDULE_PATTERN}:" in call["source_message_id"]
        for call in calendar.calls
    )


def test_succeeded_card_is_terminal():
    card = course_schedule_result_card(
        "已添加 2 项课程到日历。",
        status="succeeded",
        import_id=str(uuid.uuid4()),
    )
    assert "mindflow_action" not in json.dumps(card, ensure_ascii=False)


def test_missing_weekday_is_unfillable_v1_context():
    payload = vision_payload()
    payload["courses"][0]["weekday"] = None
    _assert_unfillable(payload, "weekday")


def test_missing_week_rule_is_unfillable_v1_context():
    payload = vision_payload()
    payload["courses"][0]["week_rule"] = None
    _assert_unfillable(payload, "week_rule")


def test_missing_actual_time_without_period_is_unfillable():
    payload = vision_payload(actual_times=False)
    payload["courses"][0]["period_start"] = None
    payload["courses"][0]["period_end"] = None
    _assert_unfillable(payload, "actual_time")


def test_backend_derives_required_context():
    payload = vision_payload(actual_times=False)
    payload["courses"][0]["weekday"] = None
    payload["courses"][0]["week_rule"] = None
    payload["missing_context"] = []
    result = ScheduleVisionResult.from_dict(payload)
    assert derive_required_context(result, semester_start_date=None) == {
        "semester_start_date",
        "weekday",
        "week_rule",
    }


def test_complete_fields_ignore_stale_model_missing_context():
    database = memory_database()
    owner = participant(database, "P108")
    payload = vision_payload()
    payload["missing_context"] = [
        "semester_start_date", "period_time_mapping", "weekday", "week_rule",
        "actual_time",
    ]
    repo = CourseScheduleImportRepository(database)
    draft = repo.create_draft(
        owner.id,
        source_message_id="stale-model-context",
        source_image_hash="d" * 64,
        vision_model="vision-model",
        result=ScheduleVisionResult.from_dict(payload),
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )
    assert draft["status"] == "pending_confirmation"
    assert draft["structured_result"]["missing_context"] == []


def test_schedule_image_pipeline_respects_max_concurrency():
    async def scenario():
        tracker = SimpleNamespace(active=0, maximum=0)

        class Resources:
            async def download_image(self, *_args):
                tracker.active += 1
                tracker.maximum = max(tracker.maximum, tracker.active)
                await asyncio.sleep(0.02)
                return SimpleNamespace(data=b"\x89PNG\r\n\x1a\n", mime_type="image/png")

        worker = _pipeline_worker(Resources(), tracker)
        await asyncio.gather(
            worker._handle_schedule_image(_image_event("evt-a", "msg-a"), uuid.uuid4()),
            worker._handle_schedule_image(_image_event("evt-b", "msg-b"), uuid.uuid4()),
        )
        assert tracker.maximum == 1

    asyncio.run(scenario())


def test_second_image_does_not_enter_expensive_pipeline_before_slot():
    async def scenario():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        entered = []

        class Resources:
            async def download_image(self, message_id, _image_key):
                entered.append(message_id)
                if message_id == "msg-first":
                    first_entered.set()
                    await release_first.wait()
                return SimpleNamespace(data=b"\x89PNG\r\n\x1a\n", mime_type="image/png")

        tracker = SimpleNamespace(active=0, maximum=0)
        worker = _pipeline_worker(Resources(), tracker)
        first = asyncio.create_task(worker._handle_schedule_image(
            _image_event("evt-first", "msg-first"), uuid.uuid4()
        ))
        await first_entered.wait()
        second = asyncio.create_task(worker._handle_schedule_image(
            _image_event("evt-second", "msg-second"), uuid.uuid4()
        ))
        await asyncio.sleep(0.01)
        assert entered == ["msg-first"]
        release_first.set()
        await asyncio.gather(first, second)
        assert entered == ["msg-first", "msg-second"]

    asyncio.run(scenario())


def test_vision_encoding_waits_for_concurrency_slot(monkeypatch):
    async def scenario():
        first_request = asyncio.Event()
        release_first = asyncio.Event()
        encoded_inputs = []
        original_encode = course_schedule_vision_module.base64.b64encode

        def tracked_encode(value):
            encoded_inputs.append(bytes(value))
            return original_encode(value)

        monkeypatch.setattr(
            course_schedule_vision_module.base64, "b64encode", tracked_encode
        )

        class BlockingTransport(httpx.AsyncBaseTransport):
            calls = 0

            async def handle_async_request(self, _request):
                self.calls += 1
                if self.calls == 1:
                    first_request.set()
                    await release_first.wait()
                return httpx.Response(200, json={
                    "choices": [{"message": {
                        "content": json.dumps(vision_payload())
                    }}]
                })

            async def aclose(self):
                return None

        service = CourseScheduleVisionService(
            "https://vision.invalid/chat",
            "secret",
            "vision-model",
            enabled=True,
            max_concurrency=1,
            transport=BlockingTransport(),
        )
        first = asyncio.create_task(service.parse(b"first", "image/png"))
        await first_request.wait()
        second = asyncio.create_task(service.parse(b"second", "image/png"))
        await asyncio.sleep(0.01)
        assert encoded_inputs == [b"first"]
        release_first.set()
        await asyncio.gather(first, second)
        assert encoded_inputs == [b"first", b"second"]

    asyncio.run(scenario())


def test_schedule_preview_limit_rejects_unseen_calendar_writes():
    payload = vision_payload()
    payload["courses"] = [
        {**payload["courses"][0], "course_name": f"课程 {index}"}
        for index in range(21)
    ]
    with pytest.raises(ScheduleVisionValidationError, match="item limit"):
        ScheduleVisionResult.from_dict(payload, max_items=80)


def test_defensive_preview_never_offers_calendar_actions_above_twenty_items():
    courses = [dict(vision_payload()["courses"][0]) for _ in range(21)]
    card = course_schedule_preview_card({
        "id": str(uuid.uuid4()),
        "status": "pending_confirmation",
        "timezone": "Asia/Shanghai",
        "semester_start_date": "2026-09-07",
        "structured_result": {"courses": courses, "missing_context": []},
        "items": [],
    })
    payload = json.dumps(card, ensure_ascii=False)
    assert "课程数量超过 20 项" in payload
    assert "course_schedule_import_confirm" not in payload
    assert "按课表周期规则添加" not in payload


def test_cancel_reply_matches_persisted_terminal_status():
    database = memory_database()
    owner = participant(database, "P110")
    repo, draft = _draft(database, owner.id)
    service = CourseScheduleImportService(repo, Calendar(), Tokens())
    asyncio.run(service.confirm(
        owner.id, draft["id"], recurrence_strategy=PRESERVE_SCHEDULE_PATTERN
    ))
    succeeded = service.cancel(owner.id, draft["id"])
    assert succeeded["status"] == "succeeded"
    assert "不能再取消" in succeeded["reply_text"]

    expired = repo.create_draft(
        owner.id,
        source_message_id="cancel-expired",
        source_image_hash="1" * 64,
        vision_model="vision-model",
        result=ScheduleVisionResult.from_dict(vision_payload()),
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
        ttl_minutes=1,
        now=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    expired_result = service.cancel(owner.id, expired["id"])
    assert expired_result["status"] == "expired"
    assert "已过期" in expired_result["reply_text"]


def _draft_with_repo(repo, participant_id, source):
    result = ScheduleVisionResult.from_dict(vision_payload())
    return repo, repo.create_draft(
        participant_id,
        source_message_id=source,
        source_image_hash="e" * 64,
        vision_model="vision-model",
        result=result,
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )


def _period_only_draft(database, participant_id, source):
    repo = CourseScheduleImportRepository(database)
    return repo, repo.create_draft(
        participant_id,
        source_message_id=source,
        source_image_hash="4" * 64,
        vision_model="vision-model",
        result=ScheduleVisionResult.from_dict(vision_payload(actual_times=False)),
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )


def _two_course_draft(database, participant_id, source):
    payload = vision_payload()
    second = dict(payload["courses"][0])
    second["course_name"] = "线性代数"
    second["weekday"] = 2
    payload["courses"].append(second)
    repo = CourseScheduleImportRepository(database)
    return repo, repo.create_draft(
        participant_id,
        source_message_id=source,
        source_image_hash="f" * 64,
        vision_model="vision-model",
        result=ScheduleVisionResult.from_dict(payload),
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )


def _expire_run_lease(database, import_id):
    with database.session() as session:
        row = session.get(CourseScheduleImport, uuid.UUID(str(import_id)))
        row.run_claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)


def _expire_draft_ttl(database, import_id):
    with database.session() as session:
        row = session.get(CourseScheduleImport, uuid.UUID(str(import_id)))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)


def _assert_unfillable(payload, expected):
    database = memory_database()
    owner = participant(database, f"PX-{expected}")
    repo = CourseScheduleImportRepository(database)
    with pytest.raises(UnfillableScheduleContextError) as captured:
        repo.create_draft(
            owner.id,
            source_message_id=f"unfillable-{expected}",
            source_image_hash="0" * 64,
            vision_model="vision-model",
            result=ScheduleVisionResult.from_dict(payload),
            timezone_name="Asia/Shanghai",
            semester_start_date=date(2026, 9, 7),
        )
    assert expected in captured.value.missing
    assert repo.get_by_source(owner.id, f"unfillable-{expected}") is None


def _image_event(event_id, message_id):
    return BotEvent(
        event_id=event_id,
        message_id=message_id,
        app_id="app",
        open_id="open",
        chat_id="chat",
        text="",
        create_time=datetime.now(timezone.utc),
        message_type="image",
        image_key="image-key",
    )


def _pipeline_worker(resources, tracker):
    payload = ScheduleVisionResult.from_dict(vision_payload())

    class Drafts:
        def get_by_source(self, *_args):
            return None

        def create_draft(self, _participant_id, **kwargs):
            tracker.active = max(0, tracker.active - 1)
            return {
                "id": kwargs["source_message_id"],
                "structured_result": kwargs["result"].to_dict(),
            }

    class Vision:
        model = "vision-model"

        async def parse(self, *_args):
            await asyncio.sleep(0.01)
            return payload

    imports = SimpleNamespace(
        drafts=Drafts(), timezone=ZoneInfo("Asia/Shanghai")
    )
    worker = BotWorker(
        asyncio.Queue(), object(), object(), object(), object(), object(), object(),
        model="fake",
        schedule_vision=Vision(),
        schedule_imports=imports,
        message_resources=resources,
        schedule_image_max_concurrency=1,
    )

    async def deliver_card(*_args):
        return None

    worker._deliver_card = deliver_card
    return worker
