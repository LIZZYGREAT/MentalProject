import asyncio
from datetime import date
import uuid

import pytest

from app.contracts.course_schedule import ScheduleVisionResult
from app.domain.course_schedule_recurrence import (
    EXPAND_ALL_OCCURRENCES,
    PRESERVE_SCHEDULE_PATTERN,
)
from app.repositories_course_schedule import (
    CourseScheduleImportRepository,
    CourseScheduleProviderIdentityConflict,
)
from app.integrations.feishu.cards import course_schedule_result_card
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

    with pytest.raises(CourseScheduleProviderIdentityConflict):
        repository.record_write_created(draft["id"], write["id"], "provider-2")

    persisted = repository.writes_for_import(draft["id"])[0]
    assert persisted["status"] == "create_identity_conflict"
    assert persisted["provider_event_id"] == "provider-1"
    assert persisted["provider_conflict_event_id"] == "provider-2"


def test_identity_conflict_is_durable_and_cannot_finalize_as_success():
    database = memory_database()
    owner = participant(database, "STAGE4-CONFLICT")
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
    repository.record_write_created(draft["id"], write["id"], "provider-1")

    with pytest.raises(CourseScheduleProviderIdentityConflict):
        repository.record_write_created(draft["id"], write["id"], "provider-2")

    final = repository.finalize_queued_import(draft["id"])
    persisted = final["writes"][0]
    assert final["status"] == "partial_failed"
    assert final["items"][0]["status"] == "failed"
    assert final["items"][0]["error_code"] == "provider_event_identity_conflict"
    assert persisted["status"] == "create_identity_conflict"
    assert persisted["provider_event_id"] == "provider-1"
    assert persisted["provider_conflict_event_id"] == "provider-2"

    result = service._result(final)
    assert result["ok"] is False
    assert result["error"] == "provider_event_identity_conflict"
    assert "暂时不要重复导入" in result["reply_text"]


def test_identity_conflict_cannot_be_blind_retried_from_confirm_card():
    database = memory_database()
    owner = participant(database, "STAGE4-CONFLICT-RETRY")
    repository, draft = _draft(database, owner.id)
    calendar = RecordingCalendar()
    service = CourseScheduleImportService(repository, calendar, Tokens())

    asyncio.run(
        service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
    )
    claimed = repository.claim_next_import()
    write = repository.claim_write(draft["id"], claimed["writes"][0]["id"])
    repository.record_write_created(draft["id"], write["id"], "provider-1")
    with pytest.raises(CourseScheduleProviderIdentityConflict):
        repository.record_write_created(draft["id"], write["id"], "provider-2")
    repository.finalize_queued_import(draft["id"])

    result = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert result["error"] == "provider_event_identity_conflict"
    assert result["status"] == "partial_failed"
    assert calendar.calls == []
    card = course_schedule_result_card(
        result["reply_text"],
        status=result["status"],
        import_id=result["import_id"],
        error=result["error"],
        recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
    )
    assert "course_schedule_import_confirm" not in str(card)


def test_runner_does_not_swallow_identity_conflict():
    database = memory_database()
    owner = participant(database, "STAGE4-RUNNER-CONFLICT")
    repository, draft = _draft(database, owner.id)
    calendar = RecordingCalendar()
    service = CourseScheduleImportService(repository, calendar, Tokens())
    runner = _runner(service)
    asyncio.run(
        service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
    )
    claimed = repository.claim_next_import()
    original_claim_write = repository.claim_write
    original_record_created = repository.record_write_created
    claim_count = 0
    record_count = 0

    def claim_write_for_duplicate(import_id, write_id):
        nonlocal claim_count
        claim_count += 1
        claimed_write = original_claim_write(import_id, write_id)
        if claimed_write is not None:
            return claimed_write
        return repository.writes_for_import(import_id)[0]

    def record_two_provider_ids(import_id, write_id, provider_event_id):
        nonlocal record_count
        record_count += 1
        return original_record_created(
            import_id,
            write_id,
            "provider-1" if record_count == 1 else "provider-2",
        )

    repository.claim_write = claim_write_for_duplicate
    repository.record_write_created = record_two_provider_ids
    duplicate_draft = dict(claimed)
    duplicate_draft["writes"] = [claimed["writes"][0], claimed["writes"][0]]

    asyncio.run(runner._run_import(duplicate_draft))

    final = repository.get(draft["id"])
    write = repository.writes_for_import(draft["id"])[0]
    assert claim_count == 2
    assert record_count == 2
    assert len(calendar.calls) == 2
    assert final["status"] == "partial_failed"
    assert write["status"] == "create_identity_conflict"
    assert write["provider_event_id"] == "provider-1"
    assert write["provider_conflict_event_id"] == "provider-2"


def test_provider_event_id_overflow_fails_closed_without_truncation():
    database = memory_database()
    owner = participant(database, "STAGE4-ID-LENGTH")
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

    with pytest.raises(ValueError, match="exceeds supported length"):
        repository.record_write_created(draft["id"], write["id"], "x" * 257)

    persisted = repository.writes_for_import(draft["id"])[0]
    assert persisted["status"] == "creating"
    assert persisted["provider_event_id"] is None


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
