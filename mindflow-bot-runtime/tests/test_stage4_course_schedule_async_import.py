import asyncio
from datetime import date
import uuid

import pytest

from app.contracts.course_schedule import ScheduleVisionResult
from app.domain.course_schedule_recurrence import (
    EXPAND_ALL_OCCURRENCES,
    PRESERVE_SCHEDULE_PATTERN,
)
from app.repositories_course_schedule import CourseScheduleImportRepository
from app.services.course_schedule_import import CourseScheduleImportService
from app.services.course_schedule_import_runner import CourseScheduleImportRunner
from helpers import memory_database, participant


def _payload(*, courses=None):
    courses = courses or [{
        "course_name": "高等数学A",
        "weekday": 1,
        "period_start": 1,
        "period_end": 2,
        "start_time": "08:00",
        "end_time": "09:35",
        "location": "A101",
        "teacher": None,
        "week_rule": {
            "start_week": 1,
            "end_week": 16,
            "odd_even": "all",
            "explicit_weeks": None,
        },
        "uncertain_fields": [],
    }]
    return {
        "document_type": "course_schedule",
        "semester_label": "2026-2027-1",
        "institution": None,
        "courses": courses,
        "missing_context": [],
        "warnings": [],
    }


def _draft(database, participant_id, source="stage4"):
    repository = CourseScheduleImportRepository(database)
    return repository, repository.create_draft(
        participant_id,
        source_message_id=source,
        source_image_hash=uuid.uuid4().hex,
        vision_model="vision-model",
        result=ScheduleVisionResult.from_dict(_payload()),
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )


class Tokens:
    def status(self, _participant_id):
        return {"connected": True, "scopes": ["calendar:calendar.event:create"]}


class RecordingCalendar:
    def __init__(self, *, missing_first=False):
        self.missing_first = missing_first
        self.calls = []

    async def _create(self, _participant_id, **kwargs):
        self.calls.append(kwargs)
        if self.missing_first and len(self.calls) == 1:
            return {}
        return {"id": f"event-{len(self.calls)}"}

    async def create_single_event(self, participant_id, **kwargs):
        return await self._create(participant_id, **kwargs)

    async def create_recurring_event(self, participant_id, **kwargs):
        return await self._create(participant_id, **kwargs)


def _runner(service, *, sender=None):
    runner = CourseScheduleImportRunner(service, sender=sender)
    service.queue_notifier = runner.wake
    return runner


def test_confirm_only_queues_and_materializes_ledger_before_runner():
    database = memory_database()
    owner = participant(database, "STAGE4-QUEUE")
    repository, draft = _draft(database, owner.id)
    calendar = RecordingCalendar()
    service = CourseScheduleImportService(repository, calendar, Tokens())
    runner = _runner(service)

    queued = asyncio.run(
        service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
    )

    assert queued["status"] == "queued"
    assert calendar.calls == []
    writes = repository.writes_for_import(draft["id"])
    assert len(writes) == 1
    assert writes[0]["status"] == "planned"

    assert asyncio.run(runner.run_once()) == 1
    assert repository.get(draft["id"])["status"] == "succeeded"
    assert repository.writes_for_import(draft["id"])[0]["status"] == "created"


def test_expand_all_occurrences_creates_sixteen_durable_ledger_rows():
    database = memory_database()
    owner = participant(database, "STAGE4-LEDGER")
    repository, draft = _draft(database, owner.id)
    calendar = RecordingCalendar()
    service = CourseScheduleImportService(repository, calendar, Tokens())
    runner = _runner(service)

    queued = asyncio.run(
        service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=EXPAND_ALL_OCCURRENCES,
        )
    )

    assert queued["status"] == "queued"
    writes = repository.writes_for_import(draft["id"])
    assert len(writes) == 16
    assert len({write["source_identity"] for write in writes}) == 16
    assert calendar.calls == []

    asyncio.run(runner.run_once())
    assert repository.get(draft["id"])["status"] == "succeeded"
    assert len(calendar.calls) == 16


def test_missing_provider_id_is_unknown_once_then_startup_recovery_retries_it():
    database = memory_database()
    owner = participant(database, "STAGE4-UNKNOWN")
    repository, draft = _draft(database, owner.id)
    calendar = RecordingCalendar(missing_first=True)
    service = CourseScheduleImportService(repository, calendar, Tokens())
    runner = _runner(service)

    asyncio.run(
        service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
    )
    asyncio.run(runner.run_once())

    failed = repository.get(draft["id"])
    write = repository.writes_for_import(draft["id"])[0]
    assert failed["status"] == "partial_failed"
    assert write["status"] == "create_outcome_unknown"
    assert write["error_code"] == "provider_event_id_missing"
    assert write["provider_event_id"] is None
    assert asyncio.run(runner.run_once()) == 0
    assert len(calendar.calls) == 1

    restarted = CourseScheduleImportRepository(database)
    restarted_service = CourseScheduleImportService(restarted, calendar, Tokens())
    restarted_runner = _runner(restarted_service)
    assert asyncio.run(restarted_runner.recover_startup()) == 1

    recovered = restarted.get(draft["id"])
    recovered_write = restarted.writes_for_import(draft["id"])[0]
    assert recovered["status"] == "succeeded"
    assert recovered_write["status"] == "created"
    assert recovered_write["provider_event_id"] == "event-2"
    assert len(calendar.calls) == 2


def test_created_write_is_monotonic_and_requires_stable_provider_identity():
    database = memory_database()
    owner = participant(database, "STAGE4-MONOTONIC")
    repository, draft = _draft(database, owner.id)
    service = CourseScheduleImportService(repository, RecordingCalendar(), Tokens())

    asyncio.run(
        service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
    )
    claimed = repository.claim_next_import()
    write = repository.claim_write(draft["id"], claimed["writes"][0]["id"])

    with pytest.raises(ValueError, match="provider event id"):
        repository.record_write_created(draft["id"], write["id"], "   ")
    repository.record_write_created(draft["id"], write["id"], "provider-1")
    repository.record_write_failure(
        draft["id"], write["id"], error_code="late_failure"
    )
    repository.record_write_created(draft["id"], write["id"], "provider-1")

    with pytest.raises(ValueError, match="provider event identity conflict"):
        repository.record_write_created(draft["id"], write["id"], "provider-2")

    persisted = repository.writes_for_import(draft["id"])[0]
    assert persisted["status"] == "created"
    assert persisted["provider_event_id"] == "provider-1"


def test_completion_card_failure_does_not_demote_durable_import():
    database = memory_database()
    owner = participant(database, "STAGE4-CARD")
    repository, draft = _draft(database, owner.id)
    calendar = RecordingCalendar()
    service = CourseScheduleImportService(repository, calendar, Tokens())

    class BrokenSender:
        def update_card(self, _message_id, _card):
            raise RuntimeError("card update failed")

    runner = _runner(service, sender=BrokenSender())
    asyncio.run(
        service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
            status_card_message_id="card-message",
        )
    )

    asyncio.run(runner.run_once())

    assert repository.get(draft["id"])["status"] == "succeeded"
    assert repository.writes_for_import(draft["id"])[0]["status"] == "created"
