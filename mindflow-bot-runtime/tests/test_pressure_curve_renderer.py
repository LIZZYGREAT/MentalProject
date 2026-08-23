from pathlib import Path
from types import SimpleNamespace
import warnings

import pytest

from app.services.curve_analysis import analyze_curve
from app.services.pressure_curve_renderer import PressureCurveRenderer


def _font_manager(*names, resolved_path_name="Noto Sans CJK JP"):
    registry = SimpleNamespace(
        ttflist=[SimpleNamespace(name=name) for name in names],
        added=[],
    )

    def addfont(path):
        registry.added.append(path)

    registry.addfont = addfont

    class FontProperties:
        def __init__(self, *, fname):
            self.fname = fname

        def get_name(self):
            return resolved_path_name

    return SimpleNamespace(fontManager=registry, FontProperties=FontProperties)


def test_debian_noto_cjk_jp_family_is_selected_before_dejavu(monkeypatch):
    monkeypatch.setattr(
        "app.services.pressure_curve_renderer._CJK_FONT_PATHS", ()
    )
    manager = _font_manager("Noto Sans CJK JP", "DejaVu Sans")

    assert PressureCurveRenderer._resolve_font_name(manager) == "Noto Sans CJK JP"


def test_noto_cjk_sc_has_priority_over_jp(monkeypatch):
    monkeypatch.setattr(
        "app.services.pressure_curve_renderer._CJK_FONT_PATHS", ()
    )
    manager = _font_manager("Noto Sans CJK JP", "Noto Sans CJK SC")

    assert PressureCurveRenderer._resolve_font_name(manager) == "Noto Sans CJK SC"


def test_existing_noto_font_file_is_registered_when_family_cache_misses(
    monkeypatch, tmp_path
):
    font_path = tmp_path / "NotoSansCJK-Regular.ttc"
    font_path.write_bytes(b"test font placeholder")
    monkeypatch.setattr(
        "app.services.pressure_curve_renderer._CJK_FONT_PATHS",
        (font_path,),
    )
    manager = _font_manager("DejaVu Sans")

    selected = PressureCurveRenderer._resolve_font_name(manager)

    assert selected == "Noto Sans CJK JP"
    assert manager.fontManager.added == [str(font_path)]


def test_missing_cjk_fonts_fall_back_without_crashing(monkeypatch, tmp_path):
    missing = tmp_path / "missing.ttc"
    monkeypatch.setattr(
        "app.services.pressure_curve_renderer._CJK_FONT_PATHS",
        (missing,),
    )
    manager = _font_manager("DejaVu Sans")

    assert PressureCurveRenderer._resolve_font_name(manager) == "DejaVu Sans"


def test_chinese_pressure_curve_png_has_no_missing_glyph_warning():
    PressureCurveRenderer._pyplot.cache_clear()
    _plt, font_name = PressureCurveRenderer._pyplot()
    if font_name == "DejaVu Sans":
        pytest.skip("host has no discoverable CJK font; Docker integration covers it")

    curve = [
        {"time": "08:00", "stress_0_10": 4.0, "vitality_0_10": 8.0},
        {"time": "12:00", "stress_0_10": 7.5, "vitality_0_10": 5.0},
        {"time": "18:00", "stress_0_10": 5.0, "vitality_0_10": 6.0},
    ]
    analysis = analyze_curve(
        curve,
        calendar_events=[
            {
                "summary": "课程与项目",
                "start_time": "2030-01-15T09:00:00+08:00",
                "end_time": "2030-01-15T10:00:00+08:00",
                "event_type": "course",
            }
        ],
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        png = PressureCurveRenderer().render(curve, analysis)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert not any("Glyph" in str(item.message) for item in caught)


def test_dockerfile_installs_fontconfig_and_refreshes_cache():
    dockerfile = (
        Path(__file__).resolve().parents[1] / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "fontconfig" in dockerfile
    assert "fonts-noto-cjk" in dockerfile
    assert "fc-cache -f" in dockerfile
    assert dockerfile.count("apt-get update") == 1
