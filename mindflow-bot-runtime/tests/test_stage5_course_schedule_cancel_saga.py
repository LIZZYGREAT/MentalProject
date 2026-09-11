import asyncio
from datetime import date, datetime, timedelta, timezone
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


def _draft(repository, participant_id, source=None, now=None, ttl_minutes=60):
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
        ttl_minutes=ttl_minutes,
        now=now,
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


def test_revert_persists_request_chat_and_pushes_completion_without_followup():
    database = memory_database()
    owner = participant(database, "STAGE5-CANCEL-NOTICE")
    calendar = Calendar()
    repository = CourseScheduleImportRepository(database)
    service = CourseScheduleImportService(repository, calendar, Tokens())

    class Sender:
        def __init__(self):
            self.cards = []

        def send_card(self, chat_id, card):
            self.cards.append((chat_id, card))
            return "om-completion"

        def send_text(self, chat_id, text):
            raise AssertionError(f"unexpected text fallback: {chat_id} {text}")

        def update_card(self, _message_id, _card):
            raise AssertionError("no status card message was supplied")

    sender = Sender()
    runner = CourseScheduleImportRunner(service, sender=sender)
    service.queue_notifier = runner.wake
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    assert asyncio.run(runner.run_once()) == 1
    assert repository.get(draft["id"])["status"] == "succeeded"

    result = service.cancel(
        owner.id,
        draft["id"],
        status_card_chat_id="oc-cancel-request",
    )
    assert result["status"] == "cancelling"
    assert repository.get(draft["id"])["status_card_chat_id"] == (
        "oc-cancel-request"
    )
    assert asyncio.run(runner.run_once()) == 1

    assert repository.get(draft["id"])["status"] == "cancelled"
    assert sender.cards[-1][0] == "oc-cancel-request"
    assert "已撤销" in str(sender.cards[-1][1])


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


def test_deleted_compensation_has_durable_rollback_refresh_after_restart():
    database = memory_database()
    owner = participant(database, "STAGE5-ROLLBACK-OUTBOX")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    asyncio.run(runner.run_once())
    service.cancel(owner.id, draft["id"])
    asyncio.run(runner.run_once())

    target = repository.compensations_for_import(draft["id"])[0]
    assert target["status"] == "deleted"
    assert target["rollback_refresh_status"] == "pending"
    assert repository.get(draft["id"])["status"] == "cancelled"

    refreshed: list[tuple[uuid.UUID, set[date]]] = []

    async def reconcile_deleted_dates(participant_id, dates):
        refreshed.append((participant_id, set(dates)))

    service.reconcile_deleted_dates = reconcile_deleted_dates
    restarted = CourseScheduleImportRunner(service)
    assert asyncio.run(restarted.run_once()) == 1

    completed = repository.compensations_for_import(draft["id"])[0]
    assert completed["rollback_refresh_status"] == "completed"
    expected_dates = {date.fromisoformat(value) for value in target["affected_dates"]}
    assert refreshed == [(owner.id, expected_dates)]


def test_rollback_refresh_failure_retries_without_reopening_provider_cleanup():
    database = memory_database()
    owner = participant(database, "STAGE5-ROLLBACK-RETRY")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    asyncio.run(runner.run_once())
    service.cancel(owner.id, draft["id"])
    asyncio.run(runner.run_once())

    async def fail_refresh(_participant_id, _dates):
        raise RuntimeError("forecast refresh unavailable")

    service.reconcile_deleted_dates = fail_refresh
    assert asyncio.run(runner.run_once()) == 1
    failed = repository.compensations_for_import(draft["id"])[0]
    assert failed["status"] == "deleted"
    assert failed["rollback_refresh_status"] == "pending"
    assert failed["rollback_refresh_error_code"] == "RuntimeError"
    assert repository.get(draft["id"])["status"] == "cancelled"

    async def succeed_refresh(_participant_id, _dates):
        return None

    service.reconcile_deleted_dates = succeed_refresh
    retry_claim = repository.claim_next_rollback_refresh(
        now=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert retry_claim is not None
    assert asyncio.run(runner._run_rollback_refresh(retry_claim)) is True
    assert repository.compensations_for_import(draft["id"])[0][
        "rollback_refresh_status"
    ] == "completed"


def test_duplicate_delete_success_does_not_reopen_completed_refresh():
    database = memory_database()
    owner = participant(database, "STAGE5-ROLLBACK-IDEMPOTENT")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    asyncio.run(runner.run_once())
    service.cancel(owner.id, draft["id"])
    asyncio.run(runner.run_once())
    target = repository.compensations_for_import(draft["id"])[0]
    claim = repository.claim_next_rollback_refresh()
    assert claim is not None
    repository.mark_rollback_refresh_completed(target["id"])
    repository.record_delete_success(target["id"])
    assert repository.compensations_for_import(draft["id"])[0][
        "rollback_refresh_status"
    ] == "completed"


def test_cancel_created_date_selector_uses_import_timezone():
    database = memory_database()
    owner = participant(database, "STAGE5-LOCAL-DATE")
    repository = CourseScheduleImportRepository(database)
    created_at = datetime(2026, 9, 8, 16, 30, tzinfo=timezone.utc)
    draft = _draft(
        repository, owner.id, now=created_at, ttl_minutes=3 * 24 * 60
    )
    query_now = created_at + timedelta(minutes=1)

    candidates = repository.recent_cancel_candidates(owner.id, now=query_now)
    assert candidates[0]["created_local_date"] == "2026-09-09"
    assert candidates[0]["created_local_datetime"] == "2026-09-09T00:30:00+08:00"
    assert candidates[0]["timezone"] == "Asia/Shanghai"
    assert candidates[0]["created_at"] == "2026-09-08T16:30:00+00:00"
    assert repository.resolve_cancel_selector(
        owner.id, {"created_date": "2026-09-09"}, now=query_now
    )["id"] == draft["id"]
    with pytest.raises(LookupError):
        repository.resolve_cancel_selector(
            owner.id, {"created_date": "2026-09-08"}, now=query_now
        )


def test_recent_cancel_candidates_explicit_now_controls_expiry():
    database = memory_database()
    owner = participant(database, "STAGE5-EXPLICIT-NOW")
    repository = CourseScheduleImportRepository(database)
    created_at = datetime(2026, 9, 8, 16, 30, tzinfo=timezone.utc)
    draft = _draft(
        repository, owner.id, now=created_at, ttl_minutes=3 * 24 * 60
    )

    before_ttl = repository.recent_cancel_candidates(
        owner.id, now=created_at + timedelta(minutes=1)
    )
    assert [candidate["id"] for candidate in before_ttl] == [draft["id"]]

    after_ttl = repository.recent_cancel_candidates(
        owner.id, now=created_at + (3 * 24 * 60 + 1) * timedelta(minutes=1)
    )
    assert after_ttl == []
    assert repository.get(draft["id"])["status"] == "expired"


def test_expired_draft_is_archived_and_omitted_from_current_cancel_candidates():
    database = memory_database()
    owner = participant(database, "STAGE5-EXPIRED-CANDIDATE")
    repository = CourseScheduleImportRepository(database)
    now = datetime.now(timezone.utc)
    expired = _draft(repository, owner.id, now=now - timedelta(hours=2))
    current = _draft(repository, owner.id, now=now - timedelta(minutes=10))

    candidates = repository.recent_cancel_candidates(owner.id)

    assert [candidate["id"] for candidate in candidates] == [current["id"]]
    assert repository.resolve_cancel_selector(owner.id, {"latest": True})["id"] == (
        current["id"]
    )
    assert repository.get(expired["id"])["status"] == "expired"
    assert repository.latest_pending_context(owner.id)["id"] == current["id"]


def test_cancelled_completion_is_restart_recoverable():
    database = memory_database()
    owner = participant(database, "STAGE5-PRESENTATION-CANCELLED")
    repository, service, _runner = _stack(database, owner, Calendar())
    draft = _draft(repository, owner.id)

    assert service.cancel(owner.id, draft["id"])["status"] == "cancelled"
    pending = repository.pending_completion_presentations()
    assert [item["id"] for item in pending] == [draft["id"]]
    assert repository.mark_completion_presentation_failed(
        draft["id"], error_code="sender_unavailable"
    )
    assert repository.pending_completion_presentations()[0]["status"] == "cancelled"
    assert repository.mark_completion_presented(draft["id"])
    assert repository.pending_completion_presentations() == []


def test_oauth_resume_reopens_cleanup_failed_presentation():
    database = memory_database()
    owner = participant(database, "STAGE5-PRESENTATION-CLEANUP")
    calendar = Calendar()
    repository, service, runner = _stack(database, owner, calendar)
    draft = _draft(repository, owner.id)
    _queue(service, owner, draft)
    asyncio.run(runner.run_once())

    calendar.fail_delete = True
    service.cancel(owner.id, draft["id"])
    asyncio.run(runner.run_once())
    assert repository.get(draft["id"])["status"] == "cleanup_failed"
    assert repository.mark_completion_presented(draft["id"])

    calendar.fail_delete = False
    assert service.resume_cleanup_for_participant(owner.id) == 1
    resumed = repository.get(draft["id"])
    assert resumed["status"] == "cancelling"
    assert resumed["completion_presented_at"] is None
    assert resumed["completion_presentation_error"] is None
