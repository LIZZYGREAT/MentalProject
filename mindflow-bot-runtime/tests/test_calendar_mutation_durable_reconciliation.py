import asyncio
from datetime import date, datetime, timedelta, timezone
import uuid

from app.models import CalendarMutationReconciliation
from app.repositories_calendar_mutation import (
    CalendarMutationReconciliationRepository,
)
from app.services.forecast_mutation_refresh import ForecastMutationRefreshQueue
from tests.helpers import memory_database, participant


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


def test_pending_reconciliation_survives_queue_shutdown_and_recovers_after_restart():
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
        assert failed["status"] == "pending"
        assert failed["attempt_count"] == 1
        assert failed["last_error_class"] == "CalendarMutationReconciliationFailed"
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
