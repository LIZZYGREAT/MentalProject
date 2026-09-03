from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.admin_web.main import create_app
from app.services.research_evaluation import ResearchEvaluationService
from app.services.workload_diagnostic_renderer import (
    WorkloadDataNotFoundError,
    WorkloadDiagnosticRenderer,
)
from helpers import memory_database, participant
from test_admin_web import login, settings


def _payload(*, ema=False, multiple_days=False):
    series = [
        {
            "participant_id": "p1",
            "local_date": "2026-09-01",
            "time": "08:00",
            "workload": 0.2,
            "forecast_stress": 4.0,
        },
        {
            "participant_id": "p1",
            "local_date": "2026-09-02" if multiple_days else "2026-09-01",
            "time": "12:00",
            "workload": 0.8,
            "forecast_stress": 7.0,
        },
    ]
    actual = []
    if ema:
        actual.append(
            {
                "participant_id": "p1",
                "local_date": "2026-09-01",
                "time": "08:00",
                "actual_stress": 5.0,
                "forecast_stress": 4.0,
                "residual": 1.0,
            }
        )
    return {
        "date_start": "2026-09-01",
        "date_end": "2026-09-02",
        "participant_code": "P001",
        "match_tolerance_minutes": 2.5,
        "series": series,
        "actual_ema": actual,
        "statistics": {},
        "event_appraisal": {"items": [], "residual_by": {}},
    }


def test_only_forecast_renders_two_primary_pngs_without_residual():
    images = WorkloadDiagnosticRenderer().render(_payload())

    assert images.demand_vs_forecast_png.startswith(b"\x89PNG")
    assert images.forecast_vs_ema_png.startswith(b"\x89PNG")
    assert images.residual_png is None


def test_forecast_plus_one_ema_renders_residual_png():
    images = WorkloadDiagnosticRenderer().render(_payload(ema=True))

    assert images.residual_png is not None
    assert images.residual_png.startswith(b"\x89PNG")


def test_multi_day_series_renders_and_workload_keeps_unit_scale():
    renderer = WorkloadDiagnosticRenderer()
    prepared = renderer._prepare(_payload(multiple_days=True))

    assert prepared.workload == (0.2, 0.8)
    assert max(prepared.workload) <= 1.0
    assert renderer.demand_vs_forecast(_payload(multiple_days=True)).startswith(b"\x89PNG")


def test_empty_series_has_explicit_empty_contract():
    with pytest.raises(WorkloadDataNotFoundError, match="workload_data_not_found"):
        WorkloadDiagnosticRenderer().render({"series": [], "actual_ema": []})


def test_admin_workload_png_routes_return_png_and_empty_residual_404(monkeypatch):
    database = memory_database()
    participant(database, "P001")
    payload = _payload()
    monkeypatch.setattr(
        ResearchEvaluationService,
        "workload_diagnostics",
        lambda *_args, **_kwargs: dict(payload),
    )
    browser = TestClient(create_app(database, settings()))
    login(browser)

    response = browser.get(
        "/admin/api/research/workload/chart/demand-vs-forecast.png"
        "?date_start=2026-09-01&date_end=2026-09-02&participant_code=P001"
    )
    missing = browser.get(
        "/admin/api/research/workload/chart/residual.png"
        "?date_start=2026-09-01&date_end=2026-09-02&participant_code=P001"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content.startswith(b"\x89PNG")
    assert missing.status_code == 404
    assert missing.json() == {"error": "workload_data_not_found"}


def test_frontend_uses_python_images_and_never_rescales_workload_to_stress():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "admin_web"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert "/research/workload/chart/${kind}.png" in source
    assert "W(t) × 10" not in source
    assert "function workloadChart(" not in source
