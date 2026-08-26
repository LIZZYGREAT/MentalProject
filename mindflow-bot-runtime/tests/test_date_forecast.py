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
)
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.forecast_initial_state import ForecastInitialStateResolver
from app.services.pressure_curve_service import (
    HistoricalForecastNotFoundError,
    PressureCurveService,
)
from app.tools.care import CareTools
from helpers import memory_database, warning_repository


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
        warnings=warning_repository(database),
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
    assert tomorrow["output"]["initial_state"]["stress_0_10"] == 4.0
    assert tomorrow["output"]["initial_state"]["vitality_0_10"] == 7.0
    assert third_day["output"]["initial_state"]["mode"] == "future_trend_default"
    assert prediction.calls[-1]["initial_state"] == {
        "stress_0_10": 4.0,
        "vitality_0_10": 7.0,
    }


def _save_forecast(database, participant_id, local_date, version, stress, vitality):
    return ForecastSnapshotRepository(database).save(
        participant_id,
        local_date,
        calendar_revision=f"calendar-{version}",
        semantic_revision=f"semantic-{version}",
        observation_revision=f"observation-{version}",
        algorithm_version="seed",
        forecast_version=version,
        semantic_status="rules_only",
        semantic_input=[],
        curve=[
            {
                "time": "23:59",
                "stress_0_10": stress,
                "vitality_0_10": vitality,
            }
        ],
        peaks=[],
        warning_windows=[],
        output={"stress_0_10": stress, "vitality_0_10": vitality},
    )


def test_today_inherits_persisted_yesterday_terminal_without_recomputing_yesterday():
    database, participant, prediction, coordinator = pipeline()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    yesterday = today - timedelta(days=1)
    source = _save_forecast(
        database, participant.id, yesterday, "yesterday-v1", 8.25, 2.5
    )

    result = asyncio.run(coordinator.ensure_forecast(participant.id, today, "test"))

    initial = result["output"]["initial_state"]
    assert initial["mode"] == "previous_day_forecast"
    assert initial["stress_0_10"] == 8.25
    assert initial["vitality_0_10"] == 2.5
    assert initial["source_local_date"] == yesterday.isoformat()
    assert initial["source_forecast_id"] == source["id"]
    assert len(prediction.calls) == 1


def test_today_without_yesterday_uses_profile_baseline():
    database, participant, _, coordinator = pipeline()
    ProfileRepository(database).save(
        participant.id,
        {
            "model_params": {
                "S_star_init": 55.0,
                "ctssm_params": {"vitality_baseline": 62.0},
            }
        },
    )
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()

    result = asyncio.run(coordinator.ensure_forecast(participant.id, today, "test"))

    initial = result["output"]["initial_state"]
    assert initial["mode"] == "profile_default"
    assert initial["stress_0_10"] == 5.5
    assert initial["vitality_0_10"] == 6.2


def test_past_curve_reads_persisted_snapshot_without_recalculation():
    database, participant, prediction, coordinator = pipeline()
    target = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=3)
    _save_forecast(database, participant.id, target, "historical-v1", 6.0, 5.0)

    class Renderer:
        def render(self, *_args, **_kwargs):
            return b"png"

    service = PressureCurveService(
        coordinator, timezone_name="Asia/Shanghai", renderer=Renderer()
    )
    view = asyncio.run(
        service.build(
            participant.id,
            target,
            reason="historical-view",
            refresh_calendar=True,
        )
    )

    assert view.forecast["forecast_version"] == "historical-v1"
    assert prediction.calls == []


def test_missing_past_curve_is_not_reconstructed_with_current_inputs():
    _, participant, prediction, coordinator = pipeline()
    target = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=3)
    service = PressureCurveService(coordinator, timezone_name="Asia/Shanghai")

    try:
        asyncio.run(
            service.build(
                participant.id,
                target,
                reason="historical-view",
                refresh_calendar=True,
            )
        )
        assert False, "expected HistoricalForecastNotFoundError"
    except HistoricalForecastNotFoundError:
        pass

    assert prediction.calls == []


def test_calendar_mutation_does_not_recompute_past_forecast():
    class Coordinator:
        def __init__(self):
            self.calls = []

        async def ensure_forecast(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    coordinator = Coordinator()
    tools = CareTools(
        None,
        None,
        None,
        None,
        "Asia/Shanghai",
        forecast_coordinator=coordinator,
        pressure_curves=object(),
    )
    yesterday = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)

    result = asyncio.run(
        tools._refresh_calendar_mutation_forecasts(
            "participant", {yesterday}, "past-calendar-mutation"
        )
    )

    assert result["forecast_refresh"] == "historical_dates_skipped"
    assert coordinator.calls == []


def test_today_calendar_mutation_invalidates_today_and_tomorrow_before_refresh():
    operations = []

    class Forecasts:
        def invalidate_for_calendar_mutation(
            self, _warnings, _participant_id, local_date, *, reason
        ):
            operations.append(("invalidate", local_date, reason))

    class Coordinator:
        warnings = object()

        def mark_dependency_dirty(
            self, _participant_id, local_date, *, reason
        ):
            operations.append(("dependency_dirty", local_date, reason))

        async def ensure_forecast(
            self, _participant_id, local_date, reason, **_kwargs
        ):
            operations.append(("refresh", local_date, reason))
            return {"local_date": local_date.isoformat()}

    tools = object.__new__(CareTools)
    tools.timezone = ZoneInfo("Asia/Shanghai")
    tools.forecast_snapshots = Forecasts()
    tools.forecast_coordinator = Coordinator()
    today = datetime.now(tools.timezone).date()
    tomorrow = today + timedelta(days=1)

    result = asyncio.run(
        tools._refresh_calendar_mutation_forecasts(
            "participant", {today}, "calendar_update_event"
        )
    )

    assert operations == [
        ("invalidate", today, "calendar_update_event"),
        ("dependency_dirty", tomorrow, "previous_day_terminal_changed"),
        ("refresh", today, "calendar_update_event"),
        ("refresh", tomorrow, "calendar_update_event"),
    ]
    assert result["forecast_refreshed_dates"] == [
        today.isoformat(), tomorrow.isoformat()
    ]


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


class _Retrospectives:
    def __init__(self, source_version, stress=5.0, vitality=4.0, revision=1):
        self.source_version = source_version
        self.stress = stress
        self.vitality = vitality
        self.revision = revision

    def latest(self, _participant_id, _local_date):
        return {
            "id": f"retro-{self.revision}",
            "source_forecast_version": self.source_version,
            "daily_review_revision": self.revision,
            "analysis": {
                "forward_terminal_state": {
                    "stress_0_10": self.stress,
                    "vitality_0_10": self.vitality,
                }
            },
        }


def _resolved_today_with_retrospective(source_version, *, forecast_version="v1"):
    database, participant, _, coordinator = pipeline()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    _save_forecast(
        database,
        participant.id,
        today - timedelta(days=1),
        forecast_version,
        8.0,
        2.5,
    )
    coordinator.retrospective_curves = _Retrospectives(source_version)
    resolved = asyncio.run(
        coordinator._resolve_initial_state(
            participant.id,
            today,
            refresh_calendar=False,
            effective_profile={},
        )
    )
    return resolved


def test_matching_retrospective_can_override_previous_terminal():
    resolved = _resolved_today_with_retrospective("v1")

    assert resolved.mode == "previous_day_daily_review"
    assert resolved.model_override == {
        "stress_0_10": 5.0,
        "vitality_0_10": 4.0,
    }


def test_stale_retrospective_is_rejected_for_newer_previous_forecast(caplog):
    resolved = _resolved_today_with_retrospective(
        "v1", forecast_version="v2"
    )

    assert resolved.mode == "previous_day_forecast"
    assert resolved.model_override == {
        "stress_0_10": 8.0,
        "vitality_0_10": 2.5,
    }
    assert "retrospective_terminal_override_stale" in caplog.text
    assert "previous_forecast_version=v2" in caplog.text
    assert "retrospective_source_forecast_version=v1" in caplog.text


def test_rebuilt_retrospective_for_current_version_is_used():
    resolved = _resolved_today_with_retrospective(
        "v2", forecast_version="v2"
    )

    assert resolved.mode == "previous_day_daily_review"
    assert resolved.source_retrospective_id == "retro-1"


def test_pressure_curve_tool_has_optional_date_schema():
    tools = CareTools(None, None, None, None, "Asia/Shanghai", object())
    registry = ToolRegistry()
    tools.register(registry)
    spec = next(item for item in registry.specs if item.name == "care_get_pressure_curve")
    assert spec.parameters["properties"]["local_date"]["format"] == "date"
    assert "local_date" not in spec.parameters.get("required", [])
