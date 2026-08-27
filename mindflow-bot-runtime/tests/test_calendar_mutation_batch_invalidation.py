from contextlib import contextmanager
from datetime import date, timedelta

import pytest

from app.models import CalendarSnapshot
from app.repositories import ForecastSnapshotRepository, ParticipantRepository
from helpers import memory_database, warning_repository


def _save_forecast(repository, participant_id, target):
    return repository.save(
        participant_id,
        target,
        calendar_revision=f"calendar-{target}",
        semantic_revision="semantic",
        algorithm_version="algorithm",
        forecast_version=f"forecast-{target}",
        semantic_status="rules_only",
        semantic_input=[],
        curve=[],
        peaks=[],
        warning_windows=[],
        output={},
    )


def test_calendar_mutation_invalidates_twenty_dates_in_one_transaction(monkeypatch):
    database = memory_database()
    participant = ParticipantRepository(database).create("BATCH-CALENDAR")
    forecasts = ForecastSnapshotRepository(database)
    warnings = warning_repository(database)
    targets = {date(2030, 1, 1) + timedelta(days=index) for index in range(20)}
    for target in targets:
        _save_forecast(forecasts, participant.id, target)

    original_session = database.session
    transaction_count = 0

    @contextmanager
    def counted_session():
        nonlocal transaction_count
        transaction_count += 1
        with original_session() as session:
            yield session

    monkeypatch.setattr(database, "session", counted_session)
    result = forecasts.invalidate_for_calendar_mutation_dates(
        warnings,
        participant.id,
        targets,
        reason="calendar_batch_test",
    )
    monkeypatch.setattr(database, "session", original_session)

    assert transaction_count == 1
    assert result == {"forecasts_invalidated": 20, "warnings_cancelled": 0}
    assert all(forecasts.latest(participant.id, target) is None for target in targets)
    with database.session() as session:
        rows = session.query(CalendarSnapshot).all()
        assert {row.local_date for row in rows} == targets
        assert all(row.snapshot_state == "mutation_refresh_pending" for row in rows)


def test_calendar_mutation_batch_rolls_back_every_date_on_mid_transaction_failure(
    monkeypatch,
):
    database = memory_database()
    participant = ParticipantRepository(database).create("BATCH-ROLLBACK")
    forecasts = ForecastSnapshotRepository(database)
    warnings = warning_repository(database)
    targets = {date(2030, 1, 15), date(2030, 1, 16)}
    for target in targets:
        _save_forecast(forecasts, participant.id, target)

    def fail_warning_cancellation(*_args, **_kwargs):
        raise RuntimeError("transient database failure")

    monkeypatch.setattr(
        warnings, "_cancel_for_forecasts_in_session", fail_warning_cancellation
    )
    with pytest.raises(RuntimeError, match="transient database failure"):
        forecasts.invalidate_for_calendar_mutation_dates(
            warnings,
            participant.id,
            targets,
            reason="calendar_batch_rollback",
        )

    assert all(forecasts.latest(participant.id, target) is not None for target in targets)
    with database.session() as session:
        assert session.query(CalendarSnapshot).count() == 0

