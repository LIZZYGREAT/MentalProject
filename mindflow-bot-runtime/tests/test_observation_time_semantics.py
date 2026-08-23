import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.repositories import ObservationRepository, ParticipantRepository
from helpers import memory_database
from mindflow_core.assessment import AssessmentModel
from tests.test_date_forecast import pipeline


SHANGHAI = ZoneInfo("Asia/Shanghai")


def checkin(observed_at: datetime, stress: float = 8.0, energy: float = 3.0):
    return {
        "type": "checkin",
        "observed_at": observed_at.isoformat(),
        "payload": {"stress_0_10": stress, "energy_0_10": energy},
    }


def predict(local_day: date, observations):
    return AssessmentModel("Asia/Shanghai").predict(
        profile={},
        observations=observations,
        calendar_events=[],
        local_date=local_day.isoformat(),
        initial_state={"stress_0_10": 4.0, "vitality_0_10": 7.0},
    )


def test_repository_filters_by_business_local_date_and_as_of():
    database = memory_database()
    participant = ParticipantRepository(database).create("OBS-DATE")
    observations = ObservationRepository(database)
    local_day = date(2026, 8, 21)
    before_midnight_utc = datetime(2026, 8, 20, 15, 30, tzinfo=timezone.utc)
    after_midnight_utc = datetime(2026, 8, 20, 16, 30, tzinfo=timezone.utc)
    observations.add(
        participant.id,
        "checkin",
        {"stress_0_10": 4},
        observed_at=before_midnight_utc,
        source_message_id="previous-local-day",
    )
    observations.add(
        participant.id,
        "checkin",
        {"stress_0_10": 7},
        observed_at=after_midnight_utc,
        source_message_id="target-local-day",
    )

    before = observations.for_local_date(
        participant.id,
        local_day,
        timezone_name="Asia/Shanghai",
        as_of=datetime(2026, 8, 21, 0, 20, tzinfo=SHANGHAI),
    )
    after = observations.for_local_date(
        participant.id,
        local_day,
        timezone_name="Asia/Shanghai",
        as_of=datetime(2026, 8, 21, 0, 40, tzinfo=SHANGHAI),
    )

    assert before == []
    assert [item["payload"]["stress_0_10"] for item in after] == [7]
    assert after[0]["observed_at"].endswith("+00:00")


def test_same_clock_observation_from_another_date_is_not_assimilated():
    target = date(2026, 8, 21)
    result = predict(
        target,
        [checkin(datetime(2026, 8, 20, 10, 30, tzinfo=SHANGHAI))],
    )

    assert not any(point["observation_assimilated"] for point in result.trajectory)


def test_future_observation_is_not_assimilated_into_historical_date():
    target = date(2026, 8, 20)
    result = predict(
        target,
        [checkin(datetime(2026, 8, 21, 10, 30, tzinfo=SHANGHAI))],
    )

    assert not any(point["observation_assimilated"] for point in result.trajectory)


def test_same_day_observations_each_apply_only_at_their_full_timestamp():
    target = date(2026, 8, 21)
    result = predict(
        target,
        [
            checkin(datetime(2026, 8, 21, 10, 30, tzinfo=SHANGHAI), 7, 4),
            checkin(datetime(2026, 8, 21, 14, 20, tzinfo=SHANGHAI), 8, 3),
        ],
    )

    applied = [
        point["time"]
        for point in result.trajectory
        if point["observation_assimilated"]
    ]
    assert applied == ["10:30", "14:20"]


def test_latest_observation_is_not_reused_as_midnight_initial_state():
    target = date(2026, 8, 21)
    observation = checkin(
        datetime(2026, 8, 21, 15, 0, tzinfo=SHANGHAI), 9, 2
    )
    without = predict(target, [])
    with_observation = AssessmentModel("Asia/Shanghai").predict(
        profile={},
        observations=[observation],
        calendar_events=[],
        local_date=target.isoformat(),
    )

    before = {
        point["time"]: point["stress_0_10"]
        for point in without.trajectory
        if point["time"] < "15:00"
    }
    observed_before = {
        point["time"]: point["stress_0_10"]
        for point in with_observation.trajectory
        if point["time"] < "15:00"
    }
    assert observed_before == before
    assert next(
        point for point in with_observation.trajectory if point["time"] == "15:00"
    )["observation_assimilated"] is True


def test_coordinator_reads_today_only_and_future_forecast_reads_none():
    database, participant, prediction_service, coordinator = pipeline()
    observations = ObservationRepository(database)
    local_now = datetime.now(SHANGHAI)
    observations.add(
        participant.id,
        "checkin",
        {"stress_0_10": 3, "energy_0_10": 8},
        observed_at=local_now - timedelta(days=1),
        source_message_id="yesterday",
    )
    observations.add(
        participant.id,
        "checkin",
        {"stress_0_10": 7, "energy_0_10": 4},
        observed_at=local_now - timedelta(minutes=1),
        source_message_id="today",
    )

    asyncio.run(
        coordinator.ensure_forecast(participant.id, local_now.date(), "today")
    )
    today_call = prediction_service.calls[-1]
    assert [item["payload"]["stress_0_10"] for item in today_call["observations"]] == [7]

    asyncio.run(
        coordinator.ensure_forecast(
            participant.id, local_now.date() + timedelta(days=2), "future"
        )
    )
    future_call = prediction_service.calls[-1]
    assert future_call["observations"] == []
    assert future_call["initial_state"] == {
        "stress_0_10": 4.0,
        "vitality_0_10": 7.0,
    }
