import asyncio
from datetime import date, datetime, timezone
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
from app.repositories_calendar_mutation import (
    CalendarMutationReconciliationRepository,
)
from app.integrations.feishu.cards import course_schedule_result_card
from app.models import CalendarMutationReconciliation, CourseScheduleImport
from app.services.course_schedule_import import CourseScheduleImportService
from app.services.course_schedule_import_runner import CourseScheduleImportRunner
from app.services.forecast_mutation_refresh import ForecastMutationRefreshQueue
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

    immediate = repository.get(draft["id"])
    assert immediate["status"] == "partial_failed"
    assert immediate["completed_at"] is None
    assert immediate["items"][0]["status"] == "failed"
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


def test_identity_conflict_after_successful_finalize_repairs_parent_atomically():
    database = memory_database()
    owner = participant(database, "STAGE4-CONFLICT-CRASH")
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
    assert repository.finalize_queued_import(draft["id"])["status"] == "succeeded"

    with pytest.raises(CourseScheduleProviderIdentityConflict):
        repository.record_write_created(draft["id"], write["id"], "provider-2")

    restarted = CourseScheduleImportRepository(database)
    restored = restarted.get(draft["id"])
    assert restored["status"] == "partial_failed"
    assert restored["completed_at"] is None
    assert restored["writes"][0]["status"] == "create_identity_conflict"


def test_confirm_repairs_stale_succeeded_parent_before_returning_result():
    database = memory_database()
    owner = participant(database, "STAGE4-CONFLICT-STALE-CONFIRM")
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
    with database.session() as session:
        row = session.get(CourseScheduleImport, uuid.UUID(draft["id"]))
        row.status = "succeeded"
        row.completed_at = row.last_progress_at

    result = asyncio.run(service.confirm(owner.id, draft["id"]))
    assert result["status"] == "partial_failed"
    assert result["error"] == "provider_event_identity_conflict"
    assert "已经添加过" not in result["reply_text"]
    assert calendar.calls == []
    assert repository.get(draft["id"])["status"] == "partial_failed"


def test_startup_normalizes_stale_succeeded_parent_without_retrying_conflict():
    database = memory_database()
    owner = participant(database, "STAGE4-CONFLICT-STARTUP")
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
    with database.session() as session:
        row = session.get(CourseScheduleImport, uuid.UUID(draft["id"]))
        row.status = "succeeded"
        row.completed_at = row.last_progress_at

    runner = _runner(service)
    assert asyncio.run(runner.recover_startup()) == 0
    assert repository.get(draft["id"])["status"] == "partial_failed"
    assert calendar.calls == []


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


def test_completion_update_failure_resends_result_card_with_retry_action():
    class Drafts:
        def __init__(self):
            self.presented = []

        def mark_completion_presented(self, import_id):
            self.presented.append(import_id)

    class Imports:
        def __init__(self):
            self.drafts = Drafts()
            self.calendar = object()

        @staticmethod
        def _result(_draft):
            return {
                "reply_text": "已添加 1 项，有 1 项没能添加。",
                "error": "calendar_not_connected",
            }

    class Sender:
        def __init__(self):
            self.cards = []
            self.texts = []

        def update_card(self, _message_id, _card):
            raise RuntimeError("original card cannot be updated")

        def send_card(self, chat_id, card):
            self.cards.append((chat_id, card))

        def send_text(self, chat_id, text):
            self.texts.append((chat_id, text))

    imports = Imports()
    sender = Sender()
    runner = CourseScheduleImportRunner(imports, sender=sender)
    draft = {
        "id": str(uuid.uuid4()),
        "status": "partial_failed",
        "recurrence_strategy": PRESERVE_SCHEDULE_PATTERN,
        "status_card_message_id": "old-card",
        "status_card_chat_id": "chat",
    }

    asyncio.run(runner._present_completion(draft))

    assert len(sender.cards) == 1
    assert sender.cards[0][0] == "chat"
    assert "course_schedule_import_confirm" in str(sender.cards[0][1])
    assert sender.texts == []
    assert imports.drafts.presented == [draft["id"]]


def test_cancelled_completion_updates_status_card_and_pushes_new_notice():
    class Drafts:
        def __init__(self):
            self.presented = []

        def mark_completion_presented(self, import_id):
            self.presented.append(import_id)

    class Imports:
        def __init__(self):
            self.drafts = Drafts()
            self.calendar = object()

        @staticmethod
        def _result(_draft):
            return {"reply_text": "这次课程表导入已撤销，相关日程已清理。"}

    class Sender:
        def __init__(self):
            self.updated = []
            self.sent = []

        def update_card(self, message_id, card):
            self.updated.append((message_id, card))

        def send_card(self, chat_id, card):
            self.sent.append((chat_id, card))

    imports = Imports()
    sender = Sender()
    runner = CourseScheduleImportRunner(imports, sender=sender)
    draft = {
        "id": str(uuid.uuid4()),
        "status": "cancelled",
        "status_card_message_id": "old-status-card",
        "status_card_chat_id": "request-chat",
    }

    asyncio.run(runner._present_completion(draft))

    assert sender.updated[0][0] == "old-status-card"
    assert sender.sent[0][0] == "request-chat"
    assert imports.drafts.presented == [draft["id"]]


class _ForecastSnapshotSpy:
    def __init__(self):
        self.invalidations = []

    def invalidate_for_calendar_mutation_dates(
        self, _warnings, participant_id, targets, *, reason
    ):
        self.invalidations.append((participant_id, set(targets), reason))


class _ForecastCoordinatorSpy:
    def __init__(self):
        self.forecasts = _ForecastSnapshotSpy()
        self.warnings = object()
        self.dependency_refresh = None
        self.refreshes = []

    async def ensure_forecast(
        self,
        participant_id,
        target,
        reason,
        *,
        refresh_calendar,
        force_followup,
    ):
        self.refreshes.append(
            (participant_id, target, reason, refresh_calendar, force_followup)
        )


def _two_occurrence_draft(database, participant_id):
    payload = _payload()
    payload["courses"][0]["week_rule"]["explicit_weeks"] = [1, 2]
    repository = CourseScheduleImportRepository(database)
    draft = repository.create_draft(
        participant_id,
        source_message_id="stage4-reconciliation",
        source_image_hash=uuid.uuid4().hex,
        vision_model="vision-model",
        result=ScheduleVisionResult.from_dict(payload),
        timezone_name="Asia/Shanghai",
        semester_start_date=date(2026, 9, 7),
    )
    return repository, draft


def _forecast_import_stack(database, owner, repository, calendar):
    reconciliations = CalendarMutationReconciliationRepository(database)
    coordinator = _ForecastCoordinatorSpy()
    refresh = ForecastMutationRefreshQueue(
        coordinator,
        reconciliations=reconciliations,
        course_schedule_imports=repository,
        recovery_poll_seconds=3600,
    )
    service = CourseScheduleImportService(
        repository,
        calendar,
        Tokens(),
        forecast_coordinator=coordinator,
        forecast_snapshots=coordinator.forecasts,
        mutation_refresh=refresh,
    )
    runner = _runner(service)
    return reconciliations, coordinator, refresh, service, runner


def test_course_schedule_all_definite_failures_bind_no_effect_and_skip_forecast():
    async def scenario():
        database = memory_database()
        owner = participant(database, "STAGE4-NO-EFFECT")
        repository, draft = _draft(database, owner.id, source="no-effect")

        class AllFailedCalendar(RecordingCalendar):
            async def _create(self, _participant_id, **kwargs):
                self.calls.append(kwargs)
                raise RuntimeError("definite provider failure")

        calendar = AllFailedCalendar()
        reconciliations, coordinator, refresh, service, runner = _forecast_import_stack(
            database, owner, repository, calendar
        )
        refresh.start()
        await service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
        await runner.run_once()
        await refresh.wait_idle()
        with database.session() as session:
            reconciliation_id = session.query(CalendarMutationReconciliation).one().id
        row = reconciliations.get(reconciliation_id)
        await refresh.close()
        return row, coordinator

    row, coordinator = asyncio.run(scenario())

    assert row["status"] == "no_effect"
    assert row["work"]["targets"] == []
    assert coordinator.forecasts.invalidations == []
    assert coordinator.refreshes == []


def test_course_schedule_partial_success_refreshes_only_created_dates():
    async def scenario():
        database = memory_database()
        owner = participant(database, "STAGE4-PARTIAL-EFFECT")
        repository, draft = _two_occurrence_draft(database, owner.id)

        class FailSecondCalendar(RecordingCalendar):
            async def _create(self, _participant_id, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 2:
                    raise RuntimeError("one write failed")
                return {"id": f"event-{len(self.calls)}"}

        calendar = FailSecondCalendar()
        reconciliations, coordinator, refresh, service, runner = _forecast_import_stack(
            database, owner, repository, calendar
        )
        refresh.start()
        await service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=EXPAND_ALL_OCCURRENCES,
        )
        await runner.run_once()
        await refresh.wait_idle()
        created_dates = repository.created_write_dates(draft["id"])
        expected_direct = service._mutation_work(created_dates)[1]
        failed_date = calendar.calls[1]["start_time"].date()
        with database.session() as session:
            reconciliation_id = session.query(CalendarMutationReconciliation).one().id
        row = reconciliations.get(reconciliation_id)
        await refresh.close()
        return row, coordinator, expected_direct, failed_date

    row, coordinator, expected_direct, failed_date = asyncio.run(scenario())

    invalidated = set().union(
        *(targets for _participant_id, targets, _reason in coordinator.forecasts.invalidations)
    )
    refreshed = {target for _participant_id, target, *_rest in coordinator.refreshes}
    bound_dates = {
        item["local_date"] for item in row["work"]["targets"]
        if item.get("requires_invalidation")
    }
    assert invalidated == expected_direct
    assert refreshed == set(row_date for row_date in expected_direct)
    assert failed_date.isoformat() not in bound_dates


def test_course_schedule_unknown_does_not_refresh_until_runner_recovery_confirms_created():
    async def scenario():
        database = memory_database()
        owner = participant(database, "STAGE4-UNKNOWN-EFFECT")
        repository, draft = _draft(database, owner.id, source="unknown-effect")
        calendar = RecordingCalendar(missing_first=True)
        reconciliations, coordinator, refresh, service, runner = _forecast_import_stack(
            database, owner, repository, calendar
        )

        await service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
        await runner.run_once()
        first_row = reconciliations.due(datetime.max.replace(tzinfo=timezone.utc))[0]
        assert coordinator.forecasts.invalidations == []
        assert coordinator.refreshes == []

        refresh.start()
        await runner.recover_startup()
        await refresh.wait_idle()
        created_dates = repository.created_write_dates(draft["id"])
        expected_direct = service._mutation_work(created_dates)[1]
        await refresh.close()
        return first_row, coordinator, expected_direct

    first_row, coordinator, expected_direct = asyncio.run(scenario())

    assert first_row["status"] == "remote_outcome_unknown"
    assert first_row["work"]["effect_dates"] == []
    assert first_row["work"]["targets"] == []
    invalidated = set().union(
        *(targets for _participant_id, targets, _reason in coordinator.forecasts.invalidations)
    )
    assert invalidated == expected_direct
    assert coordinator.refreshes


def test_course_schedule_recovery_binds_completed_ledger_after_runner_crash():
    async def scenario():
        database = memory_database()
        owner = participant(database, "STAGE4-CRASH-BIND")
        repository, draft = _draft(database, owner.id, source="crash-bind")
        calendar = RecordingCalendar()
        reconciliations, coordinator, refresh, service, _runner_instance = (
            _forecast_import_stack(database, owner, repository, calendar)
        )
        await service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
        claimed = repository.claim_next_import()
        all_dates = {
            date.fromisoformat(value)
            for write in claimed["writes"]
            for value in write["affected_dates"]
        }
        reconciliation = await service._prepare_reconciliation(
            owner.id, claimed, all_dates, {}
        )
        write = repository.claim_write(draft["id"], claimed["writes"][0]["id"])
        calendar_result = await calendar.create_recurring_event(
            owner.id,
            summary=write["summary"],
            start_time=datetime.fromisoformat(write["start_time"]),
            end_time=datetime.fromisoformat(write["end_time"]),
            description=write["description"],
            recurrence=write["recurrence"],
            source_message_id=write["source_identity"],
        )
        repository.record_write_created(
            draft["id"], write["id"], calendar_result["id"]
        )
        assert repository.finalize_queued_import(draft["id"])["status"] == "succeeded"
        with database.session() as session:
            session.get(
                CalendarMutationReconciliation,
                uuid.UUID(reconciliation["id"]),
            ).next_attempt_at = datetime.now(timezone.utc)

        refresh.start()
        await refresh.recover_now()
        await refresh.wait_idle()
        created_dates = repository.created_write_dates(draft["id"])
        expected_direct = service._mutation_work(created_dates)[1]
        recovered = reconciliations.get(reconciliation["id"])
        await refresh.close()
        return recovered, coordinator, expected_direct

    recovered, coordinator, expected_direct = asyncio.run(scenario())

    invalidated = set().union(
        *(targets for _participant_id, targets, _reason in coordinator.forecasts.invalidations)
    )
    assert recovered["status"] == "resolved"
    assert invalidated == expected_direct


def test_course_schedule_identity_conflict_uses_effect_dates_once():
    async def scenario():
        database = memory_database()
        owner = participant(database, "STAGE4-CONFLICT-EFFECT")
        repository, draft = _draft(database, owner.id, source="conflict-effect")
        calendar = RecordingCalendar()
        reconciliations, coordinator, refresh, service, _runner_instance = (
            _forecast_import_stack(database, owner, repository, calendar)
        )
        await service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
        claimed = repository.claim_next_import()
        write = repository.claim_write(draft["id"], claimed["writes"][0]["id"])
        all_dates = {
            date.fromisoformat(value) for value in write["affected_dates"]
        }
        reconciliation = await service._prepare_reconciliation(
            owner.id, repository.get(draft["id"]), all_dates, {}
        )
        repository.record_write_created(draft["id"], write["id"], "provider-a")
        with pytest.raises(CourseScheduleProviderIdentityConflict):
            repository.record_write_created(draft["id"], write["id"], "provider-b")

        refresh.start()
        effect_dates = repository.created_write_dates(draft["id"])
        await service._finish_reconciliation(
            reconciliation,
            effect_dates=effect_dates,
            outcome_unknown=False,
        )
        await service._reconcile_forecasts(
            owner.id, effect_dates, reconciliation=reconciliation
        )
        await refresh.wait_idle()
        await refresh.close()
        return coordinator, service._mutation_work(effect_dates)[1]

    coordinator, expected_direct = asyncio.run(scenario())

    invalidated = set().union(
        *(targets for _participant_id, targets, _reason in coordinator.forecasts.invalidations)
    )
    assert invalidated == expected_direct
    assert len(coordinator.forecasts.invalidations) == 1
    assert {
        target for _participant_id, target, *_rest in coordinator.refreshes
    } == expected_direct


def test_course_schedule_live_lease_blocks_recovery_before_forecast_invalidation():
    async def scenario():
        database = memory_database()
        owner = participant(database, "STAGE4-LIVE-LEASE")
        repository, draft = _draft(database, owner.id, source="live-lease")
        calendar = RecordingCalendar()
        reconciliations, coordinator, refresh, service, _runner_instance = (
            _forecast_import_stack(database, owner, repository, calendar)
        )
        await service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
        claimed = repository.claim_next_import()
        effect_dates = {
            date.fromisoformat(value)
            for write in claimed["writes"]
            for value in write["affected_dates"]
        }
        reconciliation = await service._prepare_reconciliation(
            owner.id, claimed, effect_dates, {}
        )
        claim_token = uuid.uuid4()
        live = reconciliations.claim_processing(
            reconciliation["id"], claim_token=claim_token
        )

        refresh.start()
        bound = await service._finish_reconciliation(
            live,
            effect_dates=effect_dates,
            outcome_unknown=False,
            claim_token=claim_token,
        )
        recovered = await refresh.recover_now()
        await service._reconcile_forecasts(
            owner.id, effect_dates, reconciliation=bound
        )
        await refresh.wait_idle()
        await refresh.close()
        return recovered, coordinator, service._mutation_work(effect_dates)[1]

    recovered, coordinator, expected_direct = asyncio.run(scenario())

    invalidated = set().union(
        *(targets for _participant_id, targets, _reason in coordinator.forecasts.invalidations)
    )
    assert recovered == 0
    assert invalidated == expected_direct
    assert len(coordinator.forecasts.invalidations) == 1


def test_course_schedule_recovery_owner_rejects_live_bind():
    async def scenario():
        database = memory_database()
        owner = participant(database, "STAGE4-RECOVERY-OWNER")
        repository, draft = _draft(database, owner.id, source="recovery-owner")
        calendar = RecordingCalendar()
        reconciliations, coordinator, refresh, service, _runner_instance = (
            _forecast_import_stack(database, owner, repository, calendar)
        )
        await service.confirm(
            owner.id,
            draft["id"],
            recurrence_strategy=PRESERVE_SCHEDULE_PATTERN,
        )
        claimed = repository.claim_next_import()
        effect_dates = {
            date.fromisoformat(value)
            for write in claimed["writes"]
            for value in write["affected_dates"]
        }
        reconciliation = await service._prepare_reconciliation(
            owner.id, claimed, effect_dates, {}
        )
        recovery_token = uuid.uuid4()
        recovery_claim = reconciliations.claim_processing(
            reconciliation["id"], claim_token=recovery_token
        )
        live_result = await service._finish_reconciliation(
            reconciliation,
            effect_dates=effect_dates,
            outcome_unknown=False,
            claim_token=uuid.uuid4(),
        )
        await refresh.close()
        return recovery_claim, live_result, coordinator

    recovery_claim, live_result, coordinator = asyncio.run(scenario())

    assert recovery_claim is not None
    assert live_result is None
    assert coordinator.forecasts.invalidations == []
