from datetime import date, datetime, timezone
import math
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from app.admin_web.main import create_app
from app.repositories import (
    CalendarSnapshotRepository,
    ForecastSnapshotRepository,
    ObservationRepository,
)
from app.services.pressure_curve_service import PressureCurveService
from helpers import memory_database, participant, seed_calendar_snapshot
from test_admin_web import login, settings


class _Renderer:
    def render(self, *_args, **_kwargs):
        return b"real-render-boundary"


def _curve():
    points = []
    for index in range(288):
        minute = index * 5
        hour, minute_in_hour = divmod(minute, 60)
        event_peak = 3.7 * math.exp(-((minute - 900) / 105) ** 2)
        stress = min(10, 3.4 + event_peak + 0.45 * math.sin(index / 17))
        vitality = max(0, min(10, 8.2 - minute / 340 + 0.25 * math.cos(index / 13)))
        points.append(
            {
                "time": f"{hour:02d}:{minute_in_hour:02d}",
                "stress_0_10": round(stress, 3),
                "vitality_0_10": round(vitality, 3),
                "event_stress_input": round(event_peak / 4, 3),
            }
        )
    return points


def _seeded_client():
    database = memory_database()
    user = participant(database, "P-VISUAL-001")
    local_date = date(2026, 8, 28)
    events = [
        {
            "summary": "项目验收会议",
            "start_time": "2026-08-28T14:00:00+08:00",
            "end_time": "2026-08-28T15:30:00+08:00",
            "importance": "high",
        }
    ]
    seed_calendar_snapshot(
        database,
        user.id,
        local_date,
        revision="calendar-current",
        events=events,
    )
    repository = ForecastSnapshotRepository(database)
    saved = repository.save(
        user.id,
        local_date,
        calendar_revision="calendar-current",
        semantic_revision="semantic-current",
        observation_revision="observation-current",
        algorithm_version="forecast.v4",
        forecast_version="forecast-authoritative-v1",
        semantic_status="complete",
        semantic_input=[],
        curve=_curve(),
        peaks=[{"time": "15:00", "stress_0_10": 7.2}],
        warning_windows=[
            {
                "risk_time": "2026-08-28T07:00:00+00:00",
                "target_time": "2026-08-28T06:40:00+00:00",
                "warning_level": "medium",
            }
        ],
        output={
            "classified_calendar_events": events,
            "initial_state": {"stress_0_10": 3.2, "vitality_0_10": 8.1},
            "initial_state_revision": "initial-state-v1",
            "model_family": "stress-ctssm.m1",
            "model_variant": "m1",
            "active_states": ["S", "V"],
        },
    )
    ObservationRepository(database).add(
        user.id,
        "instant_checkin",
        {"stress_0_10": 7.4, "energy_0_10": 4.3},
        observed_at=datetime(2026, 8, 28, 7, 10, tzinfo=timezone.utc),
        source_message_id="visual-checkin",
    )
    coordinator = SimpleNamespace(
        forecasts=repository,
        calendar_snapshots=CalendarSnapshotRepository(database),
    )
    pressure_curves = PressureCurveService(
        coordinator,
        timezone_name="Asia/Shanghai",
        renderer=_Renderer(),
    )
    browser = TestClient(create_app(database, settings(), pressure_curves=pressure_curves))
    login(browser)
    return browser, database, repository, user, local_date, saved


def test_admin_curve_returns_the_full_current_persisted_forecast_without_recompute():
    browser, _database, repository, user, local_date, saved = _seeded_client()

    response = browser.get(f"/admin/api/participants/P-VISUAL-001/pressure-curve/{local_date}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["forecast_id"] == saved["id"]
    assert payload["forecast_version"] == "forecast-authoritative-v1"
    assert payload["forecast_version"] == repository.latest(user.id, local_date)["forecast_version"]
    assert payload["point_count"] == len(payload["curve"]) == 288
    assert payload["curve"][0]["time"] == "00:00"
    assert payload["curve"][-1]["time"] == "23:55"
    assert payload["is_current"] is True
    assert payload["events"][0]["summary"] == "项目验收会议"
    assert payload["warnings"][0]["risk_time_local"] == "15:00"
    assert payload["instant_observations"][0]["payload"]["stress_0_10"] == 7.4


def test_admin_curve_missing_date_is_read_only_and_does_not_create_a_forecast():
    browser, _database, repository, user, local_date, _saved = _seeded_client()
    missing_date = date(2026, 8, 29)

    response = browser.get(f"/admin/api/participants/P-VISUAL-001/pressure-curve/{missing_date}")

    assert response.status_code == 404
    assert response.json() == {"error": "forecast_not_found"}
    assert repository.latest(user.id, missing_date) is None
    assert repository.latest(user.id, local_date) is not None


def test_forecast_chart_source_uses_time_coordinates_and_declares_all_overlays():
    static_dir = Path(__file__).resolve().parents[1] / "app" / "admin_web" / "static"
    chart = (static_dir / "forecast_chart.js").read_text(encoding="utf-8")
    app = (static_dir / "app.js").read_text(encoding="utf-8")

    assert "LAST_CURVE_MINUTE = 23 * 60 + 55" in chart
    assert "point.time" in chart
    assert "23:55" in chart
    assert "24:00" not in chart
    assert "PredictionRun" not in chart + app
    for required in (
        "Calendar Event",
        "Warning / Risk Window",
        "即时 Observation",
        "Daily Review 回顾估计",
        "significantChanges",
    ):
        assert required in chart
    assert "read_persisted" not in app, "the browser must use the API rather than know repository internals"
