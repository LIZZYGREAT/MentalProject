import asyncio
from datetime import date
import uuid

from app.services.forecast_mutation_refresh import ForecastMutationRefreshQueue


def test_mutation_refresh_queue_bounds_cross_participant_concurrency():
    class Coordinator:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = []

        async def ensure_forecast(self, participant_id, target, *_args, **_kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((participant_id, target))
            if self.active == 2:
                self.started.set()
            try:
                await self.release.wait()
            finally:
                self.active -= 1

    async def scenario():
        coordinator = Coordinator()
        queue = ForecastMutationRefreshQueue(coordinator, max_concurrency=2)
        queue.start()
        participants = [uuid.uuid4() for _index in range(3)]
        target = date(2030, 1, 15)
        for participant_id in participants:
            assert queue.enqueue(
                participant_id, {target: True}, reason="calendar_mutation"
            )

        await asyncio.wait_for(coordinator.started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert coordinator.max_active == 2
        assert len(coordinator.calls) == 2
        coordinator.release.set()
        await asyncio.wait_for(queue.wait_idle(), timeout=0.5)
        assert coordinator.max_active == 2
        assert len(coordinator.calls) == 3
        await queue.close()

    asyncio.run(scenario())


def test_mutation_refresh_queue_dedupes_and_preserves_strongest_calendar_refresh():
    class Coordinator:
        def __init__(self):
            self.calls = []

        async def ensure_forecast(
            self, participant_id, target, reason, *, refresh_calendar, **_kwargs
        ):
            self.calls.append(
                (participant_id, target, reason, refresh_calendar)
            )

    async def scenario():
        coordinator = Coordinator()
        queue = ForecastMutationRefreshQueue(coordinator)
        queue.start()
        participant_id = uuid.uuid4()
        target = date(2030, 1, 15)
        second_target = date(2030, 1, 16)
        assert queue.enqueue(
            participant_id, {target: False}, reason="dependency"
        )
        assert queue.enqueue(
            participant_id, {target: True}, reason="direct_calendar_mutation"
        )
        assert queue.enqueue(
            participant_id, {second_target: True}, reason="direct_calendar_mutation"
        )
        assert queue.enqueue(
            participant_id, {second_target: False}, reason="dependency"
        )
        await asyncio.wait_for(queue.wait_idle(), timeout=0.5)
        await queue.close()
        assert coordinator.calls == [
            (participant_id, target, "direct_calendar_mutation", True),
            (participant_id, second_target, "direct_calendar_mutation", True),
        ]

    asyncio.run(scenario())


def test_dependency_refresh_is_skipped_when_previous_date_rebuild_fails():
    class Coordinator:
        def __init__(self):
            self.calls = []

        async def ensure_forecast(
            self, _participant_id, target, _reason, *, refresh_calendar, **_kwargs
        ):
            self.calls.append((target, refresh_calendar))
            if target == date(2030, 1, 15):
                raise RuntimeError("source rebuild failed")

    async def scenario():
        coordinator = Coordinator()
        queue = ForecastMutationRefreshQueue(coordinator)
        queue.start()
        participant_id = uuid.uuid4()
        queue.enqueue(
            participant_id,
            {
                date(2030, 1, 15): True,
                date(2030, 1, 16): False,
                date(2030, 1, 17): True,
            },
            reason="calendar_mutation",
        )
        await asyncio.wait_for(queue.wait_idle(), timeout=0.5)
        await queue.close()
        assert coordinator.calls == [
            (date(2030, 1, 15), True),
            (date(2030, 1, 17), True),
        ]

    asyncio.run(scenario())


def test_transient_batch_invalidation_is_retried_before_forecast_refresh():
    target_dates = {date(2030, 1, 15), date(2030, 1, 16)}

    class Forecasts:
        def __init__(self):
            self.attempts = 0

        def invalidate_for_calendar_mutation_dates(
            self, _warnings, _participant_id, dates, *, reason
        ):
            self.attempts += 1
            assert set(dates) == target_dates
            assert reason == "calendar_update_event"
            if self.attempts == 1:
                raise RuntimeError("transient invalidation failure")

    class Coordinator:
        def __init__(self):
            self.forecasts = Forecasts()
            self.warnings = object()
            self.calls = []

        async def ensure_forecast(self, _participant_id, target, *_args, **_kwargs):
            self.calls.append(target)

    async def scenario():
        coordinator = Coordinator()
        queue = ForecastMutationRefreshQueue(
            coordinator, invalidation_retry_delays=(0,)
        )
        queue.start()
        participant_id = uuid.uuid4()
        assert queue.enqueue(
            participant_id,
            {target: True for target in target_dates},
            reason="calendar_update_event",
            invalidation_dates=target_dates,
        )
        await asyncio.wait_for(queue.wait_idle(), timeout=0.5)
        await queue.close()
        assert coordinator.forecasts.attempts == 2
        assert coordinator.calls == sorted(target_dates)

    asyncio.run(scenario())


def test_transient_dependency_invalidation_is_retried_before_refresh():
    source = date(2030, 1, 15)
    target = date(2030, 1, 16)

    class DependencyRefresh:
        def __init__(self):
            self.attempts = 0

        def invalidate_dependent_now(
            self, _participant_id, source_date, *, reason
        ):
            self.attempts += 1
            assert source_date == source
            assert reason == "calendar_update_event"
            if self.attempts == 1:
                raise RuntimeError("transient dependency invalidation")

    class Coordinator:
        def __init__(self):
            self.dependency_refresh = DependencyRefresh()
            self.calls = []

        async def ensure_forecast(self, _participant_id, local_date, *_args, **_kwargs):
            self.calls.append(local_date)

    async def scenario():
        coordinator = Coordinator()
        queue = ForecastMutationRefreshQueue(
            coordinator, invalidation_retry_delays=(0,)
        )
        queue.start()
        assert queue.enqueue(
            uuid.uuid4(),
            {target: False},
            reason="calendar_update_event",
            dependency_invalidation_sources={target: source},
        )
        await asyncio.wait_for(queue.wait_idle(), timeout=0.5)
        await queue.close()
        assert coordinator.dependency_refresh.attempts == 2
        assert coordinator.calls == [target]

    asyncio.run(scenario())
