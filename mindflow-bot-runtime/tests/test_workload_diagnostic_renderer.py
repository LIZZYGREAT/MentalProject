from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from app.services import workload_diagnostic_renderer as renderer_module
from app.admin_web.main import create_app
from app.services.research_evaluation import ResearchEvaluationService
from app.services.workload_diagnostic_renderer import (
    WorkloadDataNotFoundError,
    WorkloadDiagnosticRenderer,
)
from helpers import memory_database, participant
from test_admin_web import login, settings


def _payload(*, ema_count=0, multiple_days=False):
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
    if ema_count:
        actual.extend(
            [
            {
                "participant_id": "p1",
                "local_date": "2026-09-01",
                "time": "08:00",
                "actual_stress": 5.0,
                "forecast_stress": 4.0,
                "residual": 1.0,
            },
            {
                "participant_id": "p1",
                "local_date": "2026-09-01",
                "time": "12:00",
                "actual_stress": 6.0,
                "forecast_stress": 7.0,
                "residual": -1.0,
            },
            ][:ema_count]
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


def test_forecast_plus_one_ema_uses_explicit_residual_empty_contract():
    images = WorkloadDiagnosticRenderer().render(_payload(ema_count=1))

    assert images.residual_png is None


def test_forecast_plus_two_ema_renders_residual_png():
    images = WorkloadDiagnosticRenderer().render(_payload(ema_count=2))

    assert images.residual_png is not None
    assert images.residual_png.startswith(b"\x89PNG")


def test_first_and_repeated_render_use_a_font_with_required_chinese_glyphs():
    from matplotlib.ft2font import FT2Font

    renderer = WorkloadDiagnosticRenderer()
    first = renderer.demand_vs_forecast(_payload())
    repeated = renderer.demand_vs_forecast(_payload())
    _, font_name, font_path = renderer._pyplot()
    glyphs = FT2Font(font_path).get_charmap()

    assert first.startswith(b"\x89PNG")
    assert repeated.startswith(b"\x89PNG")
    assert font_name != "DejaVu Sans"
    assert all(
        ord(character) in glyphs
        for character in renderer_module._REQUIRED_CHART_GLYPHS
    )


def test_font_resolution_skips_existing_candidate_without_required_glyphs(
    monkeypatch, tmp_path
):
    unsupported = tmp_path / "candidate-a.ttf"
    supported = tmp_path / "candidate-b.ttf"
    unsupported.touch()
    supported.touch()

    class Registry:
        def __init__(self):
            self.added = []

        def addfont(self, path):
            self.added.append(path)

    registry = Registry()

    class FontProperties:
        def __init__(self, *, fname=None, family=None):
            self.fname = fname
            self.family = family

        def get_name(self):
            return Path(self.fname).stem

    class FakeFontManager:
        fontManager = registry

        @staticmethod
        def findfont(*_args, **_kwargs):
            raise ValueError("missing")

    FakeFontManager.FontProperties = FontProperties

    monkeypatch.setattr(
        renderer_module, "_CJK_FONT_PATHS", (unsupported, supported)
    )
    monkeypatch.setattr(renderer_module, "_PREFERRED_CJK_FONT_NAMES", ())
    monkeypatch.setattr(
        WorkloadDiagnosticRenderer,
        "_font_supports",
        staticmethod(lambda path, _required: Path(path) == supported),
    )

    name, path = WorkloadDiagnosticRenderer._resolve_font(FakeFontManager)

    assert name == "candidate-b"
    assert path == str(supported.resolve())
    assert registry.added == [str(supported)]


def test_font_resolution_fails_when_no_candidate_has_required_glyphs(
    monkeypatch, tmp_path
):
    candidate = tmp_path / "candidate.ttf"
    candidate.touch()

    class Registry:
        @staticmethod
        def addfont(_path):
            raise AssertionError("unsupported font must not be registered")

    class FontProperties:
        def __init__(self, *, fname=None, family=None):
            self.fname = fname
            self.family = family

    class FakeFontManager:
        fontManager = Registry()

        @staticmethod
        def findfont(*_args, **_kwargs):
            raise ValueError("missing")

    FakeFontManager.FontProperties = FontProperties

    monkeypatch.setattr(renderer_module, "_CJK_FONT_PATHS", (candidate,))
    monkeypatch.setattr(
        renderer_module, "_PREFERRED_CJK_FONT_NAMES", ("Missing CJK",)
    )
    monkeypatch.setattr(
        WorkloadDiagnosticRenderer,
        "_font_supports",
        staticmethod(lambda _path, _required: False),
    )

    with pytest.raises(
        RuntimeError, match="workload_diagnostic_cjk_font_unavailable"
    ):
        WorkloadDiagnosticRenderer._resolve_font(FakeFontManager)


def test_renderer_initialization_prewarms_font_cache_before_first_request():
    WorkloadDiagnosticRenderer._pyplot.cache_clear()

    WorkloadDiagnosticRenderer()

    assert WorkloadDiagnosticRenderer._pyplot.cache_info().currsize == 1


def test_primary_charts_share_a_wide_product_figure_proportion():
    images = WorkloadDiagnosticRenderer().render(_payload(ema_count=2))
    demand_size = Image.open(BytesIO(images.demand_vs_forecast_png)).size
    ema_size = Image.open(BytesIO(images.forecast_vs_ema_png)).size

    assert demand_size[0] == ema_size[0]
    assert demand_size[0] / demand_size[1] > 1.5
    assert ema_size[0] / ema_size[1] > 1.8


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


def test_admin_workload_png_returns_503_when_cjk_font_is_unavailable(monkeypatch):
    WorkloadDiagnosticRenderer._pyplot.cache_clear()

    def unavailable(_font_manager):
        raise RuntimeError("workload_diagnostic_cjk_font_unavailable")

    monkeypatch.setattr(
        WorkloadDiagnosticRenderer, "_resolve_font", staticmethod(unavailable)
    )
    monkeypatch.setattr(
        ResearchEvaluationService,
        "workload_diagnostics",
        lambda *_args, **_kwargs: _payload(),
    )
    database = memory_database()
    participant(database, "P001")
    browser = TestClient(create_app(database, settings()))
    login(browser)

    response = browser.get(
        "/admin/api/research/workload/chart/demand-vs-forecast.png"
        "?date_start=2026-09-01&date_end=2026-09-02&participant_code=P001"
    )

    assert response.status_code == 503
    assert response.json() == {"error": "workload_chart_font_unavailable"}


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
