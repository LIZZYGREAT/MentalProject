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
    assert "api(`/admin/api/participants/${encoded}/overview`)" in app
    assert "api(`/admin/api/participants/${code}/overview`)" in app
    assert "/overview?through=${encodeURIComponent(state.researchEnd)}" not in app


def test_authoritative_forecast_stays_server_rendered_and_responsive():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert "pressureCurveImage" in app
    assert "pressure-curve/${localDate}.png" in app
    assert "renderForecastChart" not in app
    assert ".forecast-chart-image { display:block; width:100%; max-width:100%" in css


def test_workload_charts_explain_semantics_and_use_product_cards():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert "W(t)" in app and "不是新的心理状态" in app
    assert "图 A · 任务负荷与预测压力" in app
    assert "图 B · 预测压力与 EMA 观测" in app
    assert "不表示因果效应" in app
    assert "Residual 暂无足够样本" in app
    assert "需要至少 2 个有效 EMA 匹配点" in app
    assert ".diagnostic-card" in css
    assert ".chart-query-meta" in css


def test_ridge_status_and_stage5_controls_have_dedicated_layouts():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert "function workloadRidgeCard" in app
    assert "status-badge" in app
    assert "样本不足" in app
    assert "renderKeyValueGrid(appraisal.ridge_fit||{})" not in app
    assert "'stage5-form'" in app
    assert ".stage5-form { grid-template-columns:minmax(300px,2fr) minmax(240px,1fr) minmax(160px,auto)" in css
    assert ".stage5-form .field { grid-template-rows:18px 48px 18px" in css
    assert ".stage5-form .filter-action button { min-width:160px" in css
