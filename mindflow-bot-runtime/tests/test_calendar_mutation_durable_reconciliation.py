import asyncio
from datetime import date, datetime, timedelta, timezone
import threading
import uuid
from zoneinfo import ZoneInfo

import pytest
import httpx

from app.agent.context import AgentContext
from app.integrations.feishu.calendar import (
    CalendarMutationNotSent,
    CalendarMutationOutcomeUnknown,
    CalendarMutationRejected,
    CalendarService,
)
from app.models import (
    CalendarMutationReconciliation,
    ForecastSnapshot,
    WarningSchedule,
)
from app.repositories import ForecastSnapshotRepository
from app.repositories_calendar_mutation import (
    CalendarMutationReconciliationRepository,
)
from app.services.forecast_mutation_refresh import ForecastMutationRefreshQueue
from app.tools.care import CareTools
from tests.helpers import memory_database, participant, warning_repository


class ForecastRepository:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.invalidations = []

    def invalidate_for_calendar_mutation_dates(
        self, _warnings, participant_id, targets, *, reason
    ):
        self.invalidations.append((participant_id, set(targets), reason))
        if self.fail:
            raise ConnectionError("database temporarily unavailable")


class Coordinator:
    def __init__(self, *, invalidation_fails=False, refresh_fails=False):
        self.forecasts = ForecastRepository(fail=invalidation_fails)
        self.warnings = object()
        self.refresh_fails = refresh_fails
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
        if self.refresh_fails:
            raise ConnectionError("forecast database write failed")


def _make_due(database, reconciliation_id):
    with database.session() as session:
        row = session.get(
            CalendarMutationReconciliation,
            uuid.UUID(str(reconciliation_id)),
        )
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)


def test_prepared_reconciliation_survives_queue_shutdown_and_recovers_after_restart():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-DURABLE-RESTART")
        target = date(2030, 1, 20)
        reconciliations = CalendarMutationReconciliationRepository(database)
        intent = reconciliations.create(
            person.id,
            mutation_kind="calendar_update_event",
            direct_dates={target},
            refresh_targets={target: True},
            dependency_sources={},
        )
        reconciliations.mark_remote_committed(intent["id"])
        _make_due(database, intent["id"])

        failing = Coordinator(invalidation_fails=True)
        first_process = ForecastMutationRefreshQueue(
            failing,
            reconciliations=reconciliations,
            invalidation_retry_delays=(),
            recovery_poll_seconds=3600,
        )
        first_process.start()
        await first_process.recover_now()
        await first_process.wait_idle()
        failed = reconciliations.get(intent["id"])
        assert failed["status"] == "fencing_failed"
        assert failed["attempt_count"] == 1
        assert failed["last_error_class"] == "CalendarMutationFencingFailed"
        assert failing.refreshes == []
        await first_process.close()

        _make_due(database, intent["id"])
        recovered = Coordinator()
        second_process = ForecastMutationRefreshQueue(
            recovered,
            reconciliations=reconciliations,
            invalidation_retry_delays=(),
            recovery_poll_seconds=3600,
        )
        second_process.start()
        await second_process.recover_now()
        await second_process.wait_idle()
        resolved = reconciliations.get(intent["id"])

        assert resolved["status"] == "resolved"
        assert resolved["resolved_at"] is not None
        assert recovered.forecasts.invalidations == [
            (person.id, {target}, "calendar_update_event")
        ]
        assert recovered.refreshes == [
            (person.id, target, "calendar_update_event", True, True)
        ]
        await second_process.close()

    asyncio.run(scenario())


def test_prepared_crash_window_is_conservatively_fenced_after_restart():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-PREPARED-CRASH")
        target = date(2030, 1, 21)
        reconciliations = CalendarMutationReconciliationRepository(database)
        intent = reconciliations.create(
            person.id,
            mutation_kind="calendar_delete_event",
            direct_dates={target},
            refresh_targets={target: True},
            dependency_sources={},
            operation={"operation_type": "delete", "event_id": "event-1"},
        )

        assert intent["status"] == "prepared"
        assert intent["work"]["operation"] == {
            "operation_type": "delete",
            "event_id": "event-1",
        }
        _make_due(database, intent["id"])

        recovered = Coordinator()
        restarted = ForecastMutationRefreshQueue(
            recovered,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        restarted.start()
        await restarted.recover_now()
        await restarted.wait_idle()

        recovered_row = reconciliations.get(intent["id"])
        assert recovered_row["status"] == "remote_outcome_unknown"
        assert recovered_row["resolved_at"] is None
        assert recovered_row["work"]["fenced_at"]
        assert recovered.forecasts.invalidations == [
            (person.id, {target}, "calendar_delete_event")
        ]
        assert recovered.refreshes == [
            (person.id, target, "calendar_delete_event", True, True)
        ]
        await restarted.close()

    asyncio.run(scenario())


def test_fenced_reconciliation_retries_refresh_without_repeating_invalidation():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-DURABLE-FENCED")
        target = date(2030, 2, 1)
        reconciliations = CalendarMutationReconciliationRepository(database)
        intent = reconciliations.create(
            person.id,
            mutation_kind="calendar_create_event",
            direct_dates={target},
            refresh_targets={target: True},
            dependency_sources={},
        )
        reconciliations.mark_remote_committed(intent["id"])
        _make_due(database, intent["id"])

        failing_refresh = Coordinator(refresh_fails=True)
        first_process = ForecastMutationRefreshQueue(
            failing_refresh,
            reconciliations=reconciliations,
            invalidation_retry_delays=(),
            recovery_poll_seconds=3600,
        )
        first_process.start()
        await first_process.recover_now()
        await first_process.wait_idle()
        fenced = reconciliations.get(intent["id"])
        assert fenced["status"] == "fenced"
        assert len(failing_refresh.forecasts.invalidations) == 1
        assert len(failing_refresh.refreshes) == 1
        await first_process.close()

        _make_due(database, intent["id"])
        recovered = Coordinator()
        second_process = ForecastMutationRefreshQueue(
            recovered,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        second_process.start()
        await second_process.recover_now()
        await second_process.wait_idle()

        assert reconciliations.get(intent["id"])["status"] == "resolved"
        assert recovered.forecasts.invalidations == []
        assert len(recovered.refreshes) == 1
        await second_process.close()

    asyncio.run(scenario())


def test_calendar_create_persists_preintent_before_remote_call_and_resolves():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-PREINTENT-CREATE")
        reconciliations = CalendarMutationReconciliationRepository(database)
        forecasts = ForecastSnapshotRepository(database)
        warnings = warning_repository(database)
        observed_remote_statuses = []

        class Calendar:
            async def create_event(self, _participant_id, **values):
                with database.session() as session:
                    row = session.query(CalendarMutationReconciliation).one()
                    observed_remote_statuses.append([row.status])
                    assert row.work_json["operation"]["operation_type"] == "create"
                return {
                    "id": "provider-event-1",
                    "start_time": values["start_time"].isoformat(),
                    "end_time": values["end_time"].isoformat(),
                }

        coordinator = Coordinator()
        coordinator.forecasts = forecasts
        coordinator.warnings = warnings
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        tools = CareTools(
            None,
            None,
            Calendar(),
            None,
            "Asia/Shanghai",
            coordinator,
            forecasts,
            mutation_refresh=queue,
        )
        target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
        start = datetime.combine(
            target, datetime.min.time().replace(hour=9), ZoneInfo("Asia/Shanghai")
        )
        ctx = AgentContext(
            person.id, "P", "open", "chat", "calendar-preintent", uuid.uuid4()
        )

        queue.start()
        result = await tools.create_calendar_event(
            ctx,
            {
                "summary": "课程",
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(hours=1)).isoformat(),
            },
        )
        await queue.wait_idle()
        rows = reconciliations.due(datetime.max.replace(tzinfo=timezone.utc))
        with database.session() as session:
            stored = session.query(CalendarMutationReconciliation).one()
            final_status = stored.status
        await queue.close()

        assert result["calendar_mutation"] == "succeeded"
        assert observed_remote_statuses == [["prepared"]]
        assert final_status == "resolved"
        assert rows == []

    asyncio.run(scenario())


def test_calendar_remote_failure_resolves_preintent_as_remote_failed():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-PREINTENT-FAILED")
        reconciliations = CalendarMutationReconciliationRepository(database)
        forecasts = ForecastSnapshotRepository(database)

        class Calendar:
            async def create_event(self, *_args, **_kwargs):
                with database.session() as session:
                    assert session.query(
                        CalendarMutationReconciliation
                    ).one().status == "prepared"
                raise RuntimeError("provider rejected mutation")

        coordinator = Coordinator()
        coordinator.forecasts = forecasts
        coordinator.warnings = warning_repository(database)
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        tools = CareTools(
            None,
            None,
            Calendar(),
            None,
            "Asia/Shanghai",
            coordinator,
            forecasts,
            mutation_refresh=queue,
        )
        target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
        start = datetime.combine(
            target, datetime.min.time().replace(hour=9), ZoneInfo("Asia/Shanghai")
        )
        ctx = AgentContext(
            person.id, "P", "open", "chat", "calendar-failed", uuid.uuid4()
        )

        with pytest.raises(RuntimeError, match="provider rejected"):
            await tools.create_calendar_event(
                ctx,
                {
                    "summary": "课程",
                    "start_time": start.isoformat(),
                    "end_time": (start + timedelta(hours=1)).isoformat(),
                },
            )
        with database.session() as session:
            stored = session.query(CalendarMutationReconciliation).one()
            assert stored.status == "remote_failed"
            assert stored.last_error_class == "RuntimeError"
            assert stored.resolved_at is not None
        assert reconciliations.due() == []

    asyncio.run(scenario())


def test_startup_barrier_ignores_old_process_grace_but_not_live_request_grace():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-STARTUP-BARRIER")
        reconciliations = CalendarMutationReconciliationRepository(database)
        process_started_at = datetime.now(timezone.utc)
        old_target = date(2030, 2, 2)
        live_target = date(2030, 2, 3)
        old = reconciliations.create(
            person.id,
            mutation_kind="calendar_create_event",
            direct_dates={old_target},
            refresh_targets={old_target: True},
            dependency_sources={},
            now=process_started_at - timedelta(seconds=1),
        )
        live = reconciliations.create(
            person.id,
            mutation_kind="calendar_update_event",
            direct_dates={live_target},
            refresh_targets={live_target: True},
            dependency_sources={},
            now=process_started_at + timedelta(seconds=1),
        )
        coordinator = Coordinator()
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        queue.start()

        recovered = await queue.recover_startup_fences(process_started_at)

        assert recovered == 1
        old_row = reconciliations.get(old["id"])
        assert old_row["status"] == "remote_outcome_unknown"
        assert old_row["work"]["fenced_at"]
        assert reconciliations.get(live["id"])["status"] == "prepared"
        assert coordinator.forecasts.invalidations == [
            (person.id, {old_target}, "calendar_create_event")
        ]
        await queue.close()

    asyncio.run(scenario())


def test_timeout_after_mutation_request_stays_recoverable_and_is_fenced():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-UNKNOWN-OUTCOME")
        reconciliations = CalendarMutationReconciliationRepository(database)
        forecasts = ForecastSnapshotRepository(database)

        class Calendar:
            async def create_event(self, *_args, **_kwargs):
                raise CalendarMutationOutcomeUnknown(
                    "response timed out after request dispatch",
                    request_kind="create_event",
                )

        coordinator = Coordinator()
        coordinator.forecasts = forecasts
        coordinator.warnings = warning_repository(database)
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        tools = CareTools(
            None,
            None,
            Calendar(),
            None,
            "Asia/Shanghai",
            coordinator,
            forecasts,
            mutation_refresh=queue,
        )
        target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
        start = datetime.combine(
            target, datetime.min.time().replace(hour=9), ZoneInfo("Asia/Shanghai")
        )
        ctx = AgentContext(
            person.id, "P", "open", "chat", "original-message-id", uuid.uuid4()
        )

        with pytest.raises(CalendarMutationOutcomeUnknown):
            await tools.create_calendar_event(
                ctx,
                {
                    "summary": "课程",
                    "start_time": start.isoformat(),
                    "end_time": (start + timedelta(hours=1)).isoformat(),
                },
            )

        row = reconciliations.due()[0]
        assert row["status"] == "remote_outcome_unknown"
        assert row["resolved_at"] is None
        assert row["work"]["operation"]["source_message_id"] == "original-message-id"
        assert row["last_error_class"] == "CalendarMutationOutcomeUnknown"

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
@pytest.mark.parametrize(
    ("failure_phase", "expected_error"),
    [
        ("primary_timeout", CalendarMutationNotSent),
        ("primary_503", CalendarMutationNotSent),
        ("mutation_timeout", CalendarMutationOutcomeUnknown),
        ("mutation_503", CalendarMutationOutcomeUnknown),
    ],
)
def test_calendar_service_distinguishes_preflight_from_dispatched_mutation(
    monkeypatch, operation, failure_phase, expected_error
):
    class Tokens:
        async def get_access_token(self, _participant_id):
            return "token"

    class Client:
        mutation_calls = 0

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            if url.endswith("/primary"):
                if failure_phase == "primary_timeout":
                    raise httpx.ReadTimeout(
                        "primary lookup timed out",
                        request=httpx.Request("POST", url),
                    )
                if failure_phase == "primary_503":
                    return httpx.Response(
                        503,
                        json={"code": 50301, "msg": "unavailable"},
                        request=httpx.Request("POST", url),
                    )
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"calendar": {"calendar_id": "c1"}}},
                    request=httpx.Request("POST", url),
                )
            return await self._mutation("POST", url)

        async def patch(self, url, **_kwargs):
            return await self._mutation("PATCH", url)

        async def delete(self, url, **_kwargs):
            return await self._mutation("DELETE", url)

        async def _mutation(self, method, url):
            type(self).mutation_calls += 1
            if failure_phase == "mutation_timeout":
                raise httpx.ReadTimeout(
                    "mutation timed out",
                    request=httpx.Request(method, url),
                )
            if failure_phase == "mutation_503":
                return httpx.Response(
                    503,
                    json={"code": 50301, "msg": "unavailable"},
                    request=httpx.Request(method, url),
                )
            return httpx.Response(
                200,
                json={"code": 0, "data": {"event": {"event_id": "event-1"}}},
                request=httpx.Request(method, url),
            )

    async def scenario():
        Client.mutation_calls = 0
        monkeypatch.setattr(
            "app.integrations.feishu.calendar.httpx.AsyncClient", Client
        )
        service = CalendarService(Tokens())
        participant_id = uuid.uuid4()
        start = datetime(2030, 3, 1, 9, tzinfo=timezone.utc)
        with pytest.raises(expected_error):
            if operation == "create":
                await service.create_event(
                    participant_id,
                    summary="course",
                    start_time=start,
                    end_time=start + timedelta(hours=1),
                    source_message_id="source",
                )
            elif operation == "update":
                await service.update_event(
                    participant_id, "event-1", summary="updated"
                )
            else:
                await service.delete_event(participant_id, "event-1")

        expected_calls = 1 if failure_phase.startswith("mutation_") else 0
        assert Client.mutation_calls == expected_calls

    asyncio.run(scenario())


def test_token_preflight_failure_is_definitively_not_sent(monkeypatch):
    class Tokens:
        async def get_access_token(self, _participant_id):
            raise httpx.ReadTimeout(
                "token fetch timed out",
                request=httpx.Request("POST", "https://provider/token"),
            )

    class Client:
        calls = 0

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            type(self).calls += 1
            raise AssertionError("provider must not be called after token failure")

    async def scenario():
        monkeypatch.setattr(
            "app.integrations.feishu.calendar.httpx.AsyncClient", Client
        )
        start = datetime(2030, 3, 1, 9, tzinfo=timezone.utc)
        with pytest.raises(CalendarMutationNotSent):
            await CalendarService(Tokens()).create_event(
                uuid.uuid4(),
                summary="course",
                start_time=start,
                end_time=start + timedelta(hours=1),
                source_message_id="source",
            )
        assert Client.calls == 0

    asyncio.run(scenario())


def test_calendar_http_taxonomy_distinguishes_rejection_from_unknown_5xx():
    rejected = httpx.Response(
        400,
        json={"code": 40001, "msg": "invalid"},
        request=httpx.Request("POST", "https://provider/events"),
    )
    unavailable = httpx.Response(
        503,
        json={"code": 50301, "msg": "unavailable"},
        request=httpx.Request("POST", "https://provider/events"),
    )

    with pytest.raises(CalendarMutationRejected) as rejected_error:
        CalendarService._checked(
            rejected, request_kind="create_event", mutation=True
        )
    with pytest.raises(CalendarMutationOutcomeUnknown) as unknown_error:
        CalendarService._checked(
            unavailable, request_kind="create_event", mutation=True
        )

    assert rejected_error.value.status_code == 400
    assert rejected_error.value.provider_code == 40001
    assert unknown_error.value.status_code == 503
    assert unknown_error.value.request_kind == "create_event"


def test_unknown_create_recovery_reuses_original_idempotency_source_message():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-CREATE-REPLAY")
        reconciliations = CalendarMutationReconciliationRepository(database)
        target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
        start = datetime.combine(
            target, datetime.min.time().replace(hour=9), ZoneInfo("Asia/Shanghai")
        )
        intent = reconciliations.create(
            person.id,
            mutation_kind="calendar_create_event",
            direct_dates={target},
            refresh_targets={target: True},
            dependency_sources={},
            operation={
                "operation_type": "create",
                "source_message_id": "original-idempotency-source",
                "requested": {
                    "summary": "课程",
                    "description": "",
                    "start_time": start.isoformat(),
                    "end_time": (start + timedelta(hours=1)).isoformat(),
                    "recurrence": "",
                    "reminder_minutes": None,
                },
            },
        )
        reconciliations.mark_remote_outcome_unknown(
            intent["id"], error_class="ReadTimeout"
        )
        replay_sources = []
        recovery_order = []

        class Calendar:
            async def create_event(self, _participant_id, **values):
                recovery_order.append("provider_replay")
                replay_sources.append(values["source_message_id"])
                return {"id": "provider-event-1"}

        coordinator = Coordinator()
        original_invalidate = (
            coordinator.forecasts.invalidate_for_calendar_mutation_dates
        )

        def record_invalidation(*args, **kwargs):
            recovery_order.append("local_fence")
            return original_invalidate(*args, **kwargs)

        coordinator.forecasts.invalidate_for_calendar_mutation_dates = (
            record_invalidation
        )
        coordinator.calendar = Calendar()
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        queue.start()
        await queue.recover_now()
        await queue.wait_idle()

        assert replay_sources == ["original-idempotency-source"]
        assert recovery_order[:2] == ["local_fence", "provider_replay"]
        assert reconciliations.get(intent["id"])["status"] == "resolved"
        await queue.close()

    asyncio.run(scenario())


def test_unknown_update_readback_resolves_without_duplicate_patch():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-UPDATE-READBACK")
        reconciliations = CalendarMutationReconciliationRepository(database)
        target = date(2030, 2, 5)
        start = datetime(2030, 2, 5, 1, 0, tzinfo=timezone.utc)
        requested = {
            "summary": "已更新课程",
            "description": "room 2",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
            "recurrence": "",
        }
        intent = reconciliations.create(
            person.id,
            mutation_kind="calendar_update_event",
            direct_dates={target},
            refresh_targets={target: True},
            dependency_sources={},
            operation={
                "operation_type": "update",
                "event_id": "event-2",
                "previous": {**requested, "summary": "旧课程"},
                "requested": requested,
            },
        )
        reconciliations.mark_remote_outcome_unknown(
            intent["id"], error_class="ReadTimeout"
        )

        class Calendar:
            async def get_event(self, _participant_id, _event_id):
                return {"id": "event-2", **requested}

            async def update_event(self, *_args, **_kwargs):
                raise AssertionError("matching read-back must not patch again")

        coordinator = Coordinator()
        coordinator.calendar = Calendar()
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        queue.start()
        await queue.recover_now()
        await queue.wait_idle()

        assert reconciliations.get(intent["id"])["status"] == "resolved"
        await queue.close()

    asyncio.run(scenario())


def test_unknown_delete_404_readback_is_confirmed_and_resolved():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-DELETE-READBACK")
        reconciliations = CalendarMutationReconciliationRepository(database)
        target = date(2030, 2, 6)
        intent = reconciliations.create(
            person.id,
            mutation_kind="calendar_delete_event",
            direct_dates={target},
            refresh_targets={target: True},
            dependency_sources={},
            operation={"operation_type": "delete", "event_id": "event-gone"},
        )
        reconciliations.mark_remote_outcome_unknown(
            intent["id"], error_class="ReadTimeout"
        )

        class Calendar:
            async def get_event(self, _participant_id, _event_id):
                raise CalendarMutationRejected(
                    "not found", status_code=404, provider_code=404,
                    request_kind="get_event",
                )

        coordinator = Coordinator()
        coordinator.calendar = Calendar()
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        queue.start()
        await queue.recover_now()
        await queue.wait_idle()

        assert reconciliations.get(intent["id"])["status"] == "resolved"
        await queue.close()

    asyncio.run(scenario())


def test_preflight_failure_is_terminal_and_never_enters_replay_queue():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-PREFLIGHT-NOT-SENT")
        reconciliations = CalendarMutationReconciliationRepository(database)
        forecasts = ForecastSnapshotRepository(database)

        class Calendar:
            async def create_event(self, *_args, **_kwargs):
                raise CalendarMutationNotSent(
                    "primary calendar lookup failed",
                    request_kind="primary_calendar_lookup",
                )

        coordinator = Coordinator()
        coordinator.forecasts = forecasts
        coordinator.warnings = warning_repository(database)
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        tools = CareTools(
            None, None, Calendar(), None, "Asia/Shanghai", coordinator,
            forecasts, mutation_refresh=queue,
        )
        target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
        start = datetime.combine(
            target, datetime.min.time().replace(hour=9), ZoneInfo("Asia/Shanghai")
        )
        ctx = AgentContext(
            person.id, "P", "open", "chat", "preflight-source", uuid.uuid4()
        )

        with pytest.raises(CalendarMutationNotSent):
            await tools.create_calendar_event(
                ctx,
                {
                    "summary": "课程",
                    "start_time": start.isoformat(),
                    "end_time": (start + timedelta(hours=1)).isoformat(),
                },
            )

        with database.session() as session:
            stored = session.query(CalendarMutationReconciliation).one()
            assert stored.status == "remote_failed"
            assert stored.resolved_at is not None
        assert reconciliations.due() == []
        assert coordinator.refreshes == []

    asyncio.run(scenario())


def test_cancel_after_remote_commit_is_immediately_recoverable():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-COMMIT-CANCEL")
        reconciliations = CalendarMutationReconciliationRepository(database)
        forecasts = ForecastSnapshotRepository(database)

        class Calendar:
            async def create_event(self, _participant_id, **values):
                return {
                    "id": "provider-event-cancel",
                    "start_time": values["start_time"].isoformat(),
                    "end_time": values["end_time"].isoformat(),
                }

        coordinator = Coordinator()
        coordinator.forecasts = forecasts
        coordinator.warnings = warning_repository(database)
        invalidations = []
        original_invalidate = forecasts.invalidate_for_calendar_mutation_dates

        def record_invalidation(*args, **kwargs):
            invalidations.append((args, kwargs))
            return original_invalidate(*args, **kwargs)

        forecasts.invalidate_for_calendar_mutation_dates = record_invalidation
        queue = ForecastMutationRefreshQueue(
            coordinator,
            reconciliations=reconciliations,
            recovery_poll_seconds=3600,
        )
        tools = CareTools(
            None, None, Calendar(), None, "Asia/Shanghai", coordinator,
            forecasts, mutation_refresh=queue,
        )
        reached_after_commit = asyncio.Event()
        never = asyncio.Event()

        async def cancel_point(*_args, **_kwargs):
            reached_after_commit.set()
            await never.wait()

        tools._refresh_calendar_mutation_forecasts = cancel_point
        target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
        start = datetime.combine(
            target, datetime.min.time().replace(hour=9), ZoneInfo("Asia/Shanghai")
        )
        forecast = forecasts.save(
            person.id,
            target,
            calendar_revision="before-calendar-mutation",
            semantic_revision="semantic",
            algorithm_version="test",
            forecast_version="before-calendar-mutation",
            semantic_status="rules_only",
            semantic_input=[],
            curve=[],
            peaks=[],
            warning_windows=[],
            output={},
        )
        warning_target = start.astimezone(timezone.utc)
        coordinator.warnings.sync(
            person.id,
            target,
            forecast_id=uuid.UUID(forecast["id"]),
            forecast_version=forecast["forecast_version"],
            warnings=[{
                "warning_identity": "commit-cancel-warning",
                "episode_identity": "commit-cancel-warning",
                "target_time": warning_target,
                "valid_until": warning_target + timedelta(minutes=30),
                "risk_time": warning_target + timedelta(minutes=10),
                "warning_level": "2",
                "payload": {"message": "stale warning"},
            }],
            now=datetime.now(timezone.utc),
        )
        ctx = AgentContext(
            person.id, "P", "open", "chat", "commit-cancel-source", uuid.uuid4()
        )
        task = asyncio.create_task(
            tools.create_calendar_event(
                ctx,
                {
                    "summary": "课程",
                    "start_time": start.isoformat(),
                    "end_time": (start + timedelta(hours=1)).isoformat(),
                },
            )
        )
        await asyncio.wait_for(reached_after_commit.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        due = reconciliations.due()
        assert len(due) == 1
        assert due[0]["status"] == "remote_committed"

        queue.start()
        await queue.recover_now()
        await queue.wait_idle()
        assert reconciliations.get(due[0]["id"])["status"] == "resolved"
        assert len(invalidations) == 1
        assert len(coordinator.refreshes) == 1
        with database.session() as session:
            assert session.get(
                ForecastSnapshot, uuid.UUID(forecast["id"])
            ).valid is False
            assert session.query(WarningSchedule).one().status == "cancelled"
        await queue.close()

    asyncio.run(scenario())


def test_simultaneous_recovery_paths_apply_one_effective_refresh():
    async def scenario():
        database = memory_database()
        person = participant(database, "CALENDAR-RECOVERY-CLAIM")
        reconciliations = CalendarMutationReconciliationRepository(database)
        target = date(2030, 3, 2)
        intent = reconciliations.create(
            person.id,
            mutation_kind="calendar_create_event",
            direct_dates={target},
            refresh_targets={target: True},
            dependency_sources={},
        )
        reconciliations.mark_remote_committed(intent["id"])

        entered = threading.Event()
        release = threading.Event()
        first = Coordinator()
        original_invalidate = first.forecasts.invalidate_for_calendar_mutation_dates

        def blocking_invalidate(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=3)
            return original_invalidate(*args, **kwargs)

        first.forecasts.invalidate_for_calendar_mutation_dates = blocking_invalidate
        second = Coordinator()
        first_queue = ForecastMutationRefreshQueue(
            first, reconciliations=reconciliations, recovery_poll_seconds=3600
        )
        second_queue = ForecastMutationRefreshQueue(
            second, reconciliations=reconciliations, recovery_poll_seconds=3600
        )
        first_queue.start()
        assert await asyncio.to_thread(entered.wait, 2)
        second_queue.start()
        await second_queue.recover_now()
        release.set()

        for _ in range(100):
            if reconciliations.get(intent["id"])["status"] == "resolved":
                break
            await asyncio.sleep(0.01)
        await first_queue.wait_idle()
        await second_queue.wait_idle()

        assert reconciliations.get(intent["id"])["status"] == "resolved"
        assert sum(
            len(item.forecasts.invalidations) for item in (first, second)
        ) == 1
        assert sum(len(item.refreshes) for item in (first, second)) == 1
        await first_queue.close()
        await second_queue.close()

    asyncio.run(scenario())
