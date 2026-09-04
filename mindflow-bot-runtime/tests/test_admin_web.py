from pathlib import Path
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from app.admin_web.auth import hash_password
from app.admin_web.main import create_app
from app.config import Settings
from helpers import memory_database, participant


def settings():
    return Settings.from_env(
        {
            "APP_ENV": "test",
            "FEISHU_BOT_APP_ID": "app",
            "FEISHU_BOT_APP_SECRET": "secret",
            "DEEPSEEK_API_KEY": "key",
            "DATABASE_URL": "sqlite:///:memory:",
            "TOKEN_ENCRYPTION_KEY": "test-key",
            "CLAUDE_MODEL": "primary",
            "CLAUDE_DEFAULT_OPUS_MODEL": "pro",
            "CLAUDE_DEFAULT_SONNET_MODEL": "pro",
            "CLAUDE_DEFAULT_HAIKU_MODEL": "flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "flash",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD_HASH": hash_password("correct-password"),
            "ADMIN_SESSION_SECRET": "a-long-test-session-secret",
        },
        base_dir=Path(__file__).resolve().parents[1],
    )


def client():
    database = memory_database()
    participant(database, "P001")
    return TestClient(create_app(database, settings()))


def login(client):
    response = client.post(
        "/admin/api/login",
        json={"username": "admin", "password": "correct-password"},
    )
    assert response.status_code == 200
    return response.json()


def test_admin_requires_login_and_lists_participants_after_login():
    browser = client()
    assert browser.get("/admin/api/participants").status_code == 401
    session = login(browser)
    response = browser.get("/admin/api/participants")
    assert response.status_code == 200
    assert response.json()["items"][0]["participant_code"] == "P001"
    assert session["csrf_token"]
    assert session["timezone"] == "Asia/Shanghai"
    assert len(session["business_date"]) == 10


def test_admin_frontend_and_public_health_are_available():
    browser = client()
    assert browser.get("/admin/").status_code == 200
    assert "MindFlow Admin" in browser.get("/admin/").text
    assert browser.get("/admin/api/health").json() == {
        "status": "ok",
        "database": "ok",
    }


def test_admin_exposes_participant_bound_care_timeline_and_ui_tab():
    browser = client()
    login(browser)

    response = browser.get("/admin/api/participants/P001/care-timeline")

    assert response.status_code == 200
    assert response.json() == {"preferences": None, "items": []}
    script = browser.get("/admin/static/app.js").text
    assert "Care Timeline" in script
    assert "/care-timeline" in script


def test_admin_profile_ui_has_the_four_research_layers():
    browser = client()
    script = browser.get("/admin/static/app.js").text

    assert "LAYER A · EXPLICIT" in script
    assert "LAYER B · PSYCHOMETRICS" in script
    assert "LAYER C · SLOW STATE" in script
    assert "LAYER D · LEARNED PARAMETERS" in script
    assert "标准量表与历史变化" in script
    assert "高级审计信息" in script
    assert "字段来源和更新时间保留在审计信息中" in script
    assert "layers.explicit?.data??p.profile" not in script


def test_admin_login_rejects_wrong_password_and_session_cookie_is_httponly():
    browser = client()
    bad = browser.post(
        "/admin/api/login", json={"username": "admin", "password": "wrong"}
    )
    assert bad.status_code == 401
    good = browser.post(
        "/admin/api/login",
        json={"username": "admin", "password": "correct-password"},
    )
    assert "HttpOnly" in good.headers["set-cookie"]
    assert "SameSite=strict" in good.headers["set-cookie"]


@pytest.mark.parametrize(
    "path",
    [
        "/admin/api/participants?page=abc",
        "/admin/api/participants?limit=0",
        "/admin/api/participants/P001/messages?limit=nan",
        "/admin/api/incidents?limit=999999",
    ],
)
def test_invalid_integer_query_parameters_return_400(path):
    browser = client()
    login(browser)

    response = browser.get(path)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_query_parameter"}


def test_admin_date_helpers_do_not_round_trip_calendar_dates_through_utc():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "admin_web"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert "function localDateString" in source
    assert "function shiftDate" in source
    assert "toISOString().slice(0,10)" not in source


def test_admin_curve_marks_mismatched_retrospective_source_without_overlaying_it():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "admin_web"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert "retrospective_matches_current_forecast===false" in source
    assert "Daily Review 回顾估计与当前预测的数据来源不一致" in source
    assert "请在“审计信息”中核对来源记录" in source
    assert "回顾估计基于 Forecast" not in source


def test_admin_daily_review_formats_optional_energy_and_peak_conflict():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "admin_web"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert "r.energy_consumption == null ? '未填写'" in source
    assert "r.peak_consistency===false" in source
    assert "本次回顾峰值回答与起始/收尾值存在冲突" in source
    assert "峰值锚点未用于曲线重建" in source


def test_disabled_admin_app_fails_closed():
    value = replace(settings(), admin_enabled=False)

    with pytest.raises(ValueError, match="ADMIN_ENABLED"):
        create_app(memory_database(), value)
