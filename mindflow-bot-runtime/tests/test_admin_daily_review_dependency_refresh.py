import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.testclient import TestClient

from app.admin_web.api import AdminAPI
from app.admin_web.auth import AdminSession, hash_password
from app.admin_web.main import create_app
from app.admin_web.repositories import AdminRepository
from app.config import Settings
from app.repositories import ForecastSnapshotRepository, ParticipantRepository
from app.services.forecast_dependency_refresh import ForecastDependencyRefreshService
from helpers import memory_database, warning_repository


def _settings():
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


def test_admin_slow_health_repository_does_not_block_event_loop(monkeypatch):
    repository = AdminRepository(memory_database())
    started = threading.Event()
    loop_thread = threading.get_ident()
    repository_thread = None

    def slow_health():
        nonlocal repository_thread
        repository_thread = threading.get_ident()
        started.set()
        time.sleep(0.30)
        return {"status": "ok", "database": "ok"}

    monkeypatch.setattr(repository, "health", slow_health)
    api = AdminAPI(repository, _settings(), None)
    request = Request({"type": "http", "method": "GET", "path": "/health"})

    async def scenario():
        task = asyncio.create_task(api.health(request))
        assert await asyncio.to_thread(started.wait, 1.0)
        heartbeat_started = time.monotonic()
        await asyncio.sleep(0.05)
        assert time.monotonic() - heartbeat_started < 0.2
        response = await task
        assert response.status_code == 200

    asyncio.run(scenario())
    assert repository_thread != loop_thread


def _save_forecast(repository, participant_id, target, version):
    return repository.save(
        participant_id,
        target,
        calendar_revision=version,
        semantic_revision=version,
        algorithm_version="algorithm",
        forecast_version=version,
        semantic_status="rules_only",
        semantic_input=[],
        curve=[],
        peaks=[],
        warning_windows=[],
        output={},
    )


def test_admin_lifespan_runs_dependency_refresh_for_rebuild_queue():
    database = memory_database()
    participant = ParticipantRepository(database).create("ADMIN-REBUILD")
    forecasts = ForecastSnapshotRepository(database)
    warnings = warning_repository(database)
    timezone_value = ZoneInfo("Asia/Shanghai")
    today = datetime.now(timezone_value).date()
    yesterday = today - timedelta(days=1)
    _save_forecast(forecasts, participant.id, today, "today-before-rebuild")

    refreshed = threading.Event()
    calls = []

    class Coordinator:
        async def ensure_forecast(self, participant_id, target, reason, **_kwargs):
            calls.append((participant_id, target, reason))
            _save_forecast(forecasts, participant_id, target, "today-after-rebuild")
            refreshed.set()
            return {"valid": True}

    dependency = ForecastDependencyRefreshService(
        forecasts,
        warnings,
        Coordinator(),
        timezone_name="Asia/Shanghai",
    )

    class DailyReviews:
        def rebuild(self, participant_id, source_date):
            invalidated = dependency.invalidate_dependent_now(
                participant_id,
                source_date,
                reason="admin_retrospective_rebuild",
            )
            queued = dependency.enqueue_dependent_after_source(
                participant_id,
                source_date,
                reason="admin_retrospective_rebuild",
            )
            return {"invalidated": invalidated, "queued": queued}

    app = create_app(
        database,
        _settings(),
        daily_reviews=DailyReviews(),
        dependency_refresh=dependency,
    )
    with TestClient(app) as browser:
        login = browser.post(
            "/admin/api/login",
            json={"username": "admin", "password": "correct-password"},
        )
        csrf = login.json()["csrf_token"]
        response = browser.post(
            (
                "/admin/api/participants/ADMIN-REBUILD/"
                f"retrospective-curve/{yesterday.isoformat()}/rebuild"
            ),
            headers={"x-csrf-token": csrf},
        )
        assert response.status_code == 200
        assert response.json()["invalidated"] == {
            "forecasts_invalidated": 1,
            "warnings_cancelled": 0,
        }
        assert response.json()["queued"] is True
        assert refreshed.wait(timeout=2.0)

    assert calls == [
        (participant.id, today, "admin_retrospective_rebuild")
    ]
    assert forecasts.latest(participant.id, today)["forecast_version"] == (
        "today-after-rebuild"
    )


def test_admin_rebuild_offloads_slow_reconstruction_from_event_loop(monkeypatch):
    database = memory_database()
    participant = ParticipantRepository(database).create("ADMIN-SLOW-REBUILD")
    repository = AdminRepository(database)

    class SlowDailyReviews:
        def __init__(self):
            self.thread_id = None

        def rebuild(self, _participant_id, target):
            self.thread_id = threading.get_ident()
            time.sleep(0.30)
            return {"local_date": target.isoformat()}

    daily_reviews = SlowDailyReviews()
    api = AdminAPI(
        repository,
        _settings(),
        None,
        daily_reviews=daily_reviews,
    )
    async def authorized(*_args, **_kwargs):
        return AdminSession(
            username="admin",
            expires_at=datetime.now(ZoneInfo("UTC")) + timedelta(hours=1),
            csrf_token="csrf",
            user_id=str(uuid.uuid4()),
            role="admin",
        )

    monkeypatch.setattr(api, "_authorized", authorized)
    target = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/rebuild",
            "headers": [],
            "path_params": {
                "participant_code": "ADMIN-SLOW-REBUILD",
                "local_date": target.isoformat(),
            },
        }
    )

    async def scenario():
        loop_thread = threading.get_ident()
        heartbeat = asyncio.Event()

        async def pulse():
            await asyncio.sleep(0.05)
            heartbeat.set()

        rebuild_task = asyncio.create_task(
            api.rebuild_retrospective_curve(request)
        )
        pulse_task = asyncio.create_task(pulse())
        await asyncio.wait_for(heartbeat.wait(), timeout=0.15)
        assert rebuild_task.done() is False
        response = await asyncio.wait_for(rebuild_task, timeout=0.60)
        await pulse_task
        assert response.status_code == 200
        assert daily_reviews.thread_id != loop_thread

    asyncio.run(scenario())
