from datetime import date, datetime, timezone

from app.models import StateObservation
from app.repositories import ObservationRepository
from tests.helpers import memory_database, participant


def test_local_date_as_of_requires_observed_and_created_time_before_cutoff():
    database = memory_database()
    person = participant(database, "OBS-KNOWLEDGE-TIME")
    repository = ObservationRepository(database)
    target = date(2030, 1, 15)
    cutoff = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)

    ids = [
        repository.add(
            person.id,
            "check_in",
            {"case": name},
            observed_at=observed,
            source_message_id=name,
        )
        for name, observed in (
            ("included", datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)),
            ("late-created", datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)),
            ("late-observed", datetime(2030, 1, 15, 15, 0, tzinfo=timezone.utc)),
        )
    ]
    with database.session() as session:
        session.get(StateObservation, ids[0]).created_at = datetime(
            2030, 1, 15, 12, 1, tzinfo=timezone.utc
        )
        session.get(StateObservation, ids[1]).created_at = datetime(
            2030, 1, 15, 14, 1, tzinfo=timezone.utc
        )
        session.get(StateObservation, ids[2]).created_at = datetime(
            2030, 1, 15, 12, 1, tzinfo=timezone.utc
        )

    rows = repository.for_local_date(
        person.id,
        target,
        timezone_name="Asia/Shanghai",
        as_of=cutoff,
    )

    assert [row["payload"]["case"] for row in rows] == ["included"]
