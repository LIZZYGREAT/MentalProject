import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.agent.tool_registry import ToolRegistry
from app.repositories import (
    CalendarSnapshotRepository,
    EventSemanticCacheRepository,
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
    ProfileRepository,
    WarningScheduleRepository,
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.forecast_initial_state import ForecastInitialStateResolver
from app.tools.care import CareTools
from helpers import memory_database


class Calendar:
    async def get_events(self, _participant_id, _start, _end):
        return []


class Model:
    MODEL_VERSION = "date-aware-test-v1"


class Prediction:
    model = Model()

    def __init__(self):
        self.calls = []

    def calculate(self, **kwargs):
        self.calls.append(kwargs)
        initial = kwargs.get("initial_state")
        stress = float(initial["stress_0_10"]) if initial else 6.25
        vitality = float(initial["vitality_0_10"]) if initial else 4.75
        return {
            "model_version": self.model.MODEL_VERSION,
            "local_date": kwargs["local_date"],
            "stress_0_10": stress,
            "vitality_0_10": vitality,
            "trajectory": [
                {
                    "time": "00:00",
                    "stress_0_10": stress,
                    "vitality_0_10": vitality,
                },
                {
                    "time": "23:59",
                    "stress_0_10": stress,
                    "vitality_0_10": vitality,
                },
            ],
            "alerts": [],
        }


def pipeline():
    database = memory_database()
    participants = ParticipantRepository(database)
    participant = participants.create("DATE-FORECAST")
    prediction = Prediction()
    coordinator = ForecastCoordinator(
        participants=participants,
        profiles=ProfileRepository(database),
        observations=ObservationRepository(database),
        calendar=Calendar(),
        calendar_snapshots=CalendarSnapshotRepository(database),
        semantics=EventSemanticPreprocessor(
            EventSemanticCacheRepository(database), client=None, model="rules"
        ),
        prediction=prediction,
        forecasts=ForecastSnapshotRepository(database),
        warnings=WarningScheduleRepository(database),
        timezone_name="Asia/Shanghai",
    )
    return database, participant, prediction, coordinator


def test_tomorrow_inherits_today_terminal_and_third_day_does_not_roll():
    _, participant, prediction, coordinator = pipeline()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()

    tomorrow = asyncio.run(
        coordinator.ensure_forecast(participant.id, today + timedelta(days=1), "test")
    )
    third_day = asyncio.run(
        coordinator.ensure_forecast(participant.id, today + timedelta(days=2), "test")
    )

    assert tomorrow["output"]["initial_state"]["mode"] == "previous_day_forecast"
    assert tomorrow["output"]["initial_state"]["stress_0_10"] == 6.25
    assert tomorrow["output"]["initial_state"]["vitality_0_10"] == 4.75
    assert third_day["output"]["initial_state"]["mode"] == "default"
    assert prediction.calls[-1]["initial_state"] is None


def test_tomorrow_cache_identity_tracks_today_forecast_version():
    database, participant, _, coordinator = pipeline()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    tomorrow = today + timedelta(days=1)
    first = asyncio.run(coordinator.ensure_forecast(participant.id, tomorrow, "first"))

    ObservationRepository(database).add(
        participant.id,
        "checkin",
        {"stress_0_10": 8, "energy_0_10": 3},
        observed_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        source_message_id="new-observation",
    )
    asyncio.run(coordinator.ensure_forecast(participant.id, today, "changed"))
    second = asyncio.run(coordinator.ensure_forecast(participant.id, tomorrow, "second"))

    assert second["forecast_version"] != first["forecast_version"]
    assert (
        second["output"]["initial_state_revision"]
        != first["output"]["initial_state_revision"]
    )


def test_initial_state_resolver_clamps_terminal_values():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    resolved = ForecastInitialStateResolver().resolve(
        today + timedelta(days=1),
        today,
        previous_day_forecast={
            "id": "forecast",
            "forecast_version": "version",
            "output": {"stress_0_10": 15, "vitality_0_10": -3},
        },
    )
    assert resolved.stress_0_10 == 10
    assert resolved.vitality_0_10 == 0


def test_pressure_curve_tool_has_optional_date_schema():
    tools = CareTools(None, None, None, None, None, None, "Asia/Shanghai")
    registry = ToolRegistry()
    tools.register(registry)
    spec = next(item for item in registry.specs if item.name == "care_get_pressure_curve")
    assert spec.parameters["properties"]["local_date"]["format"] == "date"
    assert "local_date" not in spec.parameters.get("required", [])
