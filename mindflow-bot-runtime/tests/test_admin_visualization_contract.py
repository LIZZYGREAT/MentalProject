from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "app" / "admin_web" / "static"


def test_shared_filter_controls_have_consistent_tokens_and_responsive_grid():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "--control-height: 48px" in css
    assert "repeat(auto-fit,minmax(210px,1fr))" in css
    assert ".participant-code-field" in css
    assert "例如 P002（可选）" in app
    assert "参与者代码" in app
    assert "participant_code（可选）" not in app


def test_complex_views_default_to_summary_and_keep_fields_and_copyable_json():
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "function renderViewToggle" in app
    assert "['summary','摘要']" in app
    assert "['fields','字段']" in app
    assert "['json','JSON']" in app
    assert "Copy JSON" in app
    assert "原始 JSON · 完整审计数据" in app
    assert 'careEffects:"summary"' in app


def test_care_effect_and_overview_copy_preserve_research_boundaries():
    app = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "该结果用于描述关联，不解释为因果效应。" in app
    assert "仅描述性散点，不拟合因果趋势。" in app
    assert "内部研究风险提示，不是诊断" in app
    assert "PARTICIPANT OVERVIEW V2" in app
    assert "source_field" in app
    assert "/overview?through=" in app


def test_authoritative_forecast_stays_server_rendered_and_responsive():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert "pressureCurveImage" in app
    assert "pressure-curve/${localDate}.png" in app
    assert "renderForecastChart" not in app
    assert ".forecast-chart-image { display:block; width:100%; max-width:100%" in css
