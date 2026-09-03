from pathlib import Path
from types import SimpleNamespace
import warnings

import pytest

from app.services.curve_analysis import analyze_curve
from app.services.pressure_curve_renderer import PressureCurveRenderer
from settings.visual_defaults import EVENT_COLOR_MAP


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


def test_chart_matches_sqlite_auth_deployment_colors_and_uses_formal_copy():
    curve = [
        {
            "time": "08:00",
            "stress_0_10": 4.0,
            "vitality_0_10": 8.0,
            "confidence_0_1": 0.3,
            "event_stress_input": 0.2,
            "anticipatory_input": 0.1,
            "post_event_input": 0.0,
        },
        {
            "time": "12:00",
            "stress_0_10": 7.5,
            "vitality_0_10": 5.0,
            "confidence_0_1": 0.8,
            "event_stress_input": 0.7,
            "anticipatory_input": 0.4,
            "post_event_input": 0.3,
        },
        {
            "time": "18:00",
            "stress_0_10": 5.0,
            "vitality_0_10": 6.0,
            "confidence_0_1": 0.4,
            "event_stress_input": 0.1,
            "anticipatory_input": 0.0,
            "post_event_input": 0.2,
        },
    ]
    analysis = analyze_curve(curve)
    renderer = PressureCurveRenderer()
    plt, _font_name = renderer._pyplot()

    figure = renderer._draw_core_plot(curve, analysis)
    try:
        stress_axis = next(
            axis for axis in figure.axes if axis.get_ylabel() == "心理压力（0–100）"
        )
        input_axis = next(
            axis
            for axis in figure.axes
            if axis.get_ylabel() == "压力影响强度（0–1）"
        )
        confidence_axis = next(
            axis
            for axis in figure.axes
            if axis.get_ylabel() == "预测置信度（0–1）"
        )
        stress_lines = {line.get_label(): line for line in stress_axis.lines}
        input_lines = {line.get_label(): line for line in input_axis.lines}
        confidence_lines = {
            line.get_label(): line for line in confidence_axis.lines
        }

        assert figure._suptitle.get_text() == "今日压力趋势与影响因素"
        assert stress_lines["压力值 S(t)"].get_color() == "royalblue"
        assert stress_lines["压力值 S(t)"].get_linewidth() == 2.5
        assert stress_lines["压力基准值=50"].get_color() == "gray"
        assert stress_lines["压力基准值=50"].get_linestyle() == ":"
        assert stress_lines["压力关注线=70"].get_color() == "red"
        assert stress_lines["压力关注线=70"].get_linestyle() == "--"
        assert input_lines["事件影响"].get_color() == "#dc2626"
        assert input_lines["事前影响"].get_color() == "#d97706"
        assert input_lines["事后影响"].get_color() == "#7c3aed"
        assert confidence_lines["预测置信度"].get_color() == "orange"
        assert confidence_lines["预测置信度"].get_alpha() == 0.60

        visible_text = "\n".join(
            [figure._suptitle.get_text()]
            + [axis.get_ylabel() for axis in figure.axes]
            + [line.get_label() for axis in figure.axes for line in axis.lines]
        )
        assert not any(
            token in visible_text
            for token in ("M0", "M3", "真实预测", "公式输入", "S_eq")
        )
    finally:
        plt.close(figure)


def test_dynamic_vitality_chart_keeps_legacy_primary_palette():
    curve = [
        {
            "time": "08:00",
            "stress_0_10": 4.0,
            "vitality_0_10": 8.0,
            "confidence_0_1": 0.3,
        },
        {
            "time": "12:00",
            "stress_0_10": 7.5,
            "vitality_0_10": 5.0,
            "confidence_0_1": 0.8,
        },
    ]
    analysis = analyze_curve(curve)
    renderer = PressureCurveRenderer()
    plt, _font_name = renderer._pyplot()
    model_output = {"model_variant": "m3", "active_states": ["S", "V", "F"]}

    figure = renderer._draw_core_plot(curve, analysis, model_output)
    try:
        vitality_axis = next(
            axis for axis in figure.axes if axis.get_ylabel() == "活力值（0–100）"
        )
        vitality_lines = {line.get_label(): line for line in vitality_axis.lines}

        assert figure._suptitle.get_text() == "今日压力与活力趋势"
        assert vitality_lines["活力值 V(t)"].get_color() == "mediumseagreen"
        assert vitality_lines["活力值 V(t)"].get_linewidth() == 2.5
        assert vitality_lines["低活力关注线=25"].get_color() == "crimson"
        assert vitality_lines["低活力关注线=25"].get_linestyle() == "-."
    finally:
        plt.close(figure)


def test_full_day_dense_events_warnings_and_optional_fields_render_safely():
    curve = [
        {
            "time": f"{minute // 60:02d}:{minute % 60:02d}",
            "stress_0_10": 4.0 + (minute / 1435) * 3.0,
        }
        for minute in range(0, 1440, 5)
    ]
    events = [
        {
            "summary": f"密集日程 {index}",
            "start_time": f"2030-01-15T{index % 24:02d}:00:00+08:00",
            "end_time": f"2030-01-15T{(index + 1) % 24:02d}:00:00+08:00",
            "event_type": "other",
            "importance": "high" if index % 4 == 0 else "normal",
        }
        for index in range(24)
    ]
    analysis = analyze_curve(
        curve,
        calendar_events=events,
        warning_windows=[
            {
                "risk_time": "2030-01-15T18:00:00+08:00",
                "warning_level": "high",
                "payload": {"type": "高风险关注"},
            }
        ],
    )

    renderer = PressureCurveRenderer()
    png = renderer.render(curve, analysis, stress_only=True)
    figure = renderer._draw_core_plot(curve, analysis, stress_only=True)
    plt, _font_name = renderer._pyplot()
    try:
        axis = figure.axes[0]
        assert curve[0]["time"] == "00:00"
        assert curve[-1]["time"] == "23:55"
        assert axis.get_xlim()[0] <= 0
        assert axis.get_xlim()[1] >= 23.9
        assert any(text.get_text() == "高风险关注" for text in axis.texts)
        assert png.startswith(b"\x89PNG")
    finally:
        plt.close(figure)


def test_historical_chart_does_not_draw_a_false_current_time_marker():
    curve = [
        {"time": "00:00", "stress_0_10": 4.0},
        {"time": "23:55", "stress_0_10": 5.0},
    ]
    renderer = PressureCurveRenderer()
    figure = renderer._draw_core_plot(
        curve,
        analyze_curve(curve),
        {"_render_is_today": False},
        stress_only=True,
    )
    plt, _font_name = renderer._pyplot()
    try:
        labels = [line.get_label() for line in figure.axes[0].lines]
        assert "当前时刻" not in labels
    finally:
        plt.close(figure)


def test_event_palette_matches_sqlite_auth_deployment_branch():
    assert EVENT_COLOR_MAP == {
        "course": ("#4169E1", "课程"),
        "task": ("#DC143C", "任务"),
        "sleep": ("#191970", "睡眠"),
        "nap": ("#20B2AA", "午休"),
        "meal": ("#3CB371", "就餐"),
        "rest": ("#BDB76B", "休息"),
        "gym": ("#FF8C00", "运动"),
        "library": ("#8A2BE2", "自习"),
    }


def test_dockerfile_installs_fontconfig_and_refreshes_cache():
    dockerfile = (
        Path(__file__).resolve().parents[1] / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "fontconfig" in dockerfile
    assert "fonts-noto-cjk" in dockerfile
    assert "fc-cache -f" in dockerfile
    assert dockerfile.count("apt-get update") == 1
