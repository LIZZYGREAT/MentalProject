from pathlib import Path

from starlette.testclient import TestClient

from app.admin_web.auth import hash_password
from app.admin_web.main import create_app
from app.config import Settings
from helpers import memory_database, participant


def settings():
    return Settings.from_env(
        {
            "APP_ENV": "test",
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
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


def test_admin_frontend_and_public_health_are_available():
    browser = client()
    assert browser.get("/admin/").status_code == 200
    assert "MindFlow Admin" in browser.get("/admin/").text
    assert browser.get("/admin/api/health").json() == {
        "status": "ok",
        "database": "ok",
    }


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
