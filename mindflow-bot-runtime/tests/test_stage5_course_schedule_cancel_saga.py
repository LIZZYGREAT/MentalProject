import asyncio
from datetime import date
import uuid

import pytest

from app.contracts.course_schedule import ScheduleVisionResult
from app.domain.course_schedule_recurrence import PRESERVE_SCHEDULE_PATTERN
from app.integrations.feishu.calendar import CalendarMutationRejected
from app.repositories_course_schedule import (
    CourseScheduleImportRepository,
    CourseScheduleProviderIdentityConflict,
)
from app.services.course_schedule_import import CourseScheduleImportService
from app.services.course_schedule_import_runner import CourseScheduleImportRunner
from helpers import memory_database, participant


def _draft(repository, participant_id, source=None):
    result = ScheduleVisionResult.from_dict({
        "document_type": "course_schedule",
        "semester_label": "2026-2027-1",
        "institution": None,
        "courses": [{
            "course_name": "高等数学A",
            "weekday": 1,
            "period_start": 1,
            "period_end": 2,
            "start_time": "08:00",
            "end_time": "09:35",
            "location": "A101",
            "week_rule": {
                "start_week": 1,
                "end_week": 16,
                "odd_even": "all",
                "explicit_weeks": None,
            },
            "uncertain_fields": [],
        }],
        "missing_context": [],
        "warnings": [],
    })
    return repository.create_draft(
        participant_id,
        source_message_id=source or uuid.uuid4().hex,
        source_image_hash=uuid.uuid4().hex,
        vision_model="test",
        result=result,
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )


class Tokens:
    def status(self, _participant_id):
        return {
            "connected": True,
            "scopes": ["calendar:calendar.event:create"],
        }


class Calendar:
    def __init__(self, *, fail_delete=False):
        self.events = {}
        self.creates = []
        self.deletes = []
        self.fail_delete = fail_delete
        self._next_id = 0

    async def create_single_event(self, _participant_id, **kwargs):
        self._next_id += 1
        event_id = f"event-{self._next_id}"
        self.creates.append(kwargs)
        self.events[event_id] = dict(kwargs)
        return {"id": event_id}

    async def create_recurring_event(self, participant_id, **kwargs):
        return await self.create_single_event(participant_id, **kwargs)

    async def delete_event(self, _participant_id, event_id):
        self.deletes.append(event_id)
        if self.fail_delete:
            raise PermissionError("calendar authorization has expired")
        self.events.pop(event_id, None)
        return {"id": event_id, "deleted": True}

    async def get_event(self, _participant_id, event_id):
        if event_id not in self.events:
            raise CalendarMutationRejected(
                "not found", status_code=404, request_kind="get_event"
            )
        return {"id": event_id}


def _stack(database, owner, calendar):
    repository = CourseScheduleImportRepository(database)
    service = CourseScheduleImportService(repository, calendar, Tokens())
    runner = CourseScheduleImportRunner(service)
    service.queue_notifier = runner.wake
    return repository, service, runner


def _queue(service, owner, draft):
    return asyncio.run(service.confirm(
        owner.id,
        draft["id"],
        recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
    ))


def test_pending_cancel_has_no_provider_effect():
    database = memory_database()
    owner = participant(database, "STAGE5-PENDING")
    repository, service, runner = _stack(database, owner, Calendar())
    draft = _draft(repository, owner.id)

    result = service.cancel(owner.id, draft["id"])

    assert result["status"] == "cancelled"
    assert result["cancel_mode"] == "before_write"
    assert asyncio.run(runner.run_once()) == 0


def test_queued_cancel_marks_planned_writes_without_delete():
    database = memory_database()
    owner = participant(database, "STAGE5-QUEUED")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)

    result = service.cancel(owner.id, draft["id"])

    assert result["status"] == "cancelled"
    assert repository.writes_for_import(draft["id"])[0]["status"] == "create_cancelled"
    assert asyncio.run(runner.run_once()) == 0
    assert calendar.creates == []
    assert calendar.deletes == []


def test_cancellation_fence_rejects_late_confirmation_claim():
    database = memory_database()
    owner = participant(database, "STAGE5-FENCE")
    repository, service, _runner = _stack(database, owner, Calendar())
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    queued = repository.claim_next_import()
    assert queued is not None
    assert repository.claim_write(draft["id"], queued["writes"][0]["id"]) is not None

    assert service.cancel(owner.id, draft["id"])["status"] == "cancelling"
    claimed = repository.begin_confirmation(owner.id, draft["id"])

    assert claimed["claimed"] is False
    assert claimed["status"] == "cancelling"


def test_completed_revert_deletes_only_ledger_identity_and_finalizes():
    database = memory_database()
    owner = participant(database, "STAGE5-REVERT")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    assert asyncio.run(runner.run_once()) == 1
    created_id = next(iter(calendar.events))

    cancelling = service.cancel(owner.id, draft["id"])
    assert cancelling["status"] == "cancelling"
    assert len(repository.compensations_for_import(draft["id"])) == 1

    assert asyncio.run(runner.run_once()) == 1
    assert calendar.deletes == [created_id]
    assert repository.get(draft["id"])["status"] == "cancelled"
    assert calendar.events == {}


def test_identity_conflict_materializes_and_deletes_both_provider_ids():
    database = memory_database()
    owner = participant(database, "STAGE5-CONFLICT")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    claimed = repository.claim_next_import()
    write = repository.claim_write(draft["id"], claimed["writes"][0]["id"])
    repository.record_write_created(draft["id"], write["id"], "provider-a")
    with pytest.raises(CourseScheduleProviderIdentityConflict):
        repository.record_write_created(draft["id"], write["id"], "provider-b")

    result = service.cancel(owner.id, draft["id"])
    assert result["status"] == "cancelling"
    assert {
        target["provider_event_id"]
        for target in repository.compensations_for_import(draft["id"])
    } == {"provider-a", "provider-b"}
    asyncio.run(runner.run_once())
    assert set(calendar.deletes) == {"provider-a", "provider-b"}
    assert repository.get(draft["id"])["status"] == "cancelled"


def test_oauth_failure_is_cleanup_failed_and_resume_is_recoverable():
    database = memory_database()
    owner = participant(database, "STAGE5-OAUTH")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    asyncio.run(runner.run_once())

    calendar.fail_delete = True
    service.cancel(owner.id, draft["id"])
    asyncio.run(runner.run_once())
    assert repository.get(draft["id"])["status"] == "cleanup_failed"
    assert repository.get(draft["id"])["cleanup_error_code"] == "calendar_not_connected"

    calendar.fail_delete = False
    assert service.resume_cleanup_for_participant(owner.id) == 1
    asyncio.run(runner.run_once())
    assert repository.get(draft["id"])["status"] == "cancelled"


def test_cancellation_replays_unknown_create_with_same_identity_before_delete():
    database = memory_database()
    owner = participant(database, "STAGE5-UNKNOWN")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    claimed = repository.claim_next_import()
    write = repository.claim_write(draft["id"], claimed["writes"][0]["id"])
    service.cancel(owner.id, draft["id"])
    repository.record_write_failure(
        draft["id"], write["id"], error_code="timeout", outcome_unknown=True
    )

    asyncio.run(runner.run_once())
    assert len(calendar.creates) == 1
    assert len(calendar.deletes) == 1
    assert repository.get(draft["id"])["status"] == "cancelled"


def test_stale_deleting_target_becomes_unknown_then_readback_deletes():
    database = memory_database()
    owner = participant(database, "STAGE5-DELETE-UNKNOWN")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    asyncio.run(runner.run_once())
    service.cancel(owner.id, draft["id"])
    target = repository.claim_next_compensation()
    assert target["status"] == "deleting"

    from datetime import datetime, timedelta, timezone

    repository.recover_stale_cancellation_work(
        now=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert repository.compensations_for_import(draft["id"])[0]["status"] == (
        "delete_outcome_unknown"
    )
    asyncio.run(runner.run_once())
    assert repository.get(draft["id"])["status"] == "cancelled"
