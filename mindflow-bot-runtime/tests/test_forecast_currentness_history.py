from datetime import date, datetime, timezone

from app.models import ForecastCurrentnessEvent
from app.repositories import ForecastSnapshotRepository
from tests.helpers import memory_database, participant, warning_repository


def _save(repository, participant_id, target, version):
    return repository.save(
        participant_id,
        target,
        calendar_revision="calendar",
        semantic_revision="semantic",
        algorithm_version="mindflow-ctssm-runtime-v7",
        forecast_version=version,
        semantic_status="complete",
        semantic_input=[],
        curve=[{"time": "23:55", "stress_0_10": 3, "vitality_0_10": 5}],
        peaks=[],
        warning_windows=[],
        output={"stress_0_10": 4, "vitality_0_10": 5},
    )


def test_current_at_reconstructs_activate_supersede_reactivate_history():
    database = memory_database()
    person = participant(database, "CURRENTNESS-HISTORY")
    target = date(2030, 1, 15)
    repository = ForecastSnapshotRepository(database)

    v1 = _save(repository, person.id, target, "v1")
    v2 = _save(repository, person.id, target, "v2")
    reactivated = _save(repository, person.id, target, "v1")
    with database.session() as session:
        events = session.query(ForecastCurrentnessEvent).order_by(
            ForecastCurrentnessEvent.id
        ).all()
        activation_times = [
            event.occurred_at for event in events if event.event_type == "activated"
        ]
    t1, t2, t3 = activation_times

    assert reactivated["id"] == v1["id"]
    assert repository.current_at(person.id, target, t1)["id"] == v1["id"]
    assert repository.current_at(person.id, target, t2)["id"] == v2["id"]
    assert repository.current_at(person.id, target, t3)["id"] == v1["id"]


def test_current_at_returns_none_after_explicit_invalidation_without_replacement():
    database = memory_database()
    person = participant(database, "CURRENTNESS-INVALIDATED")
    target = date(2030, 1, 15)
    repository = ForecastSnapshotRepository(database)
    saved = _save(repository, person.id, target, "v1")
    repository.invalidate_current_for_date(
        warning_repository(database),
        person.id,
        target,
        reason="test_dependency_changed",
    )
    with database.session() as session:
        events = session.query(ForecastCurrentnessEvent).order_by(
            ForecastCurrentnessEvent.id
        ).all()
        before = events[0].occurred_at
        after = events[-1].occurred_at

    assert repository.current_at(person.id, target, before)["id"] == saved["id"]
    assert repository.current_at(person.id, target, after) is None
