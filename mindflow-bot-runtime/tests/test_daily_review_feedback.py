from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import asyncio
import uuid

from app.admin_web.auth import hash_password
from app.admin_web.main import create_app
from app.config import Settings
from app.integrations.feishu.cards import daily_review_card
from app.repositories import ForecastSnapshotRepository, ObservationRepository
from app.repositories_daily_review import (
    DailyReviewResponseRepository,
    DailyReviewScheduleRepository,
    RetrospectiveCurveRepository,
)
from app.services.daily_review_scheduler import DailyReviewScheduler
from app.services.daily_review_service import DailyReviewService
from app.services.forecast_initial_state import ForecastInitialStateResolver
from helpers import memory_database, participant
from starlette.testclient import TestClient


def _settings() -> Settings:
    return Settings.from_env({
        "APP_ENV": "test", "FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret",
        "DEEPSEEK_API_KEY": "key", "DATABASE_URL": "sqlite:///:memory:",
        "TOKEN_ENCRYPTION_KEY": "test-key", "CLAUDE_MODEL": "primary",
        "CLAUDE_DEFAULT_OPUS_MODEL": "pro", "CLAUDE_DEFAULT_SONNET_MODEL": "pro",
        "CLAUDE_DEFAULT_HAIKU_MODEL": "flash", "CLAUDE_CODE_SUBAGENT_MODEL": "flash",
        "ADMIN_USERNAME": "root-admin", "ADMIN_PASSWORD_HASH": hash_password("correct-password"),
        "ADMIN_SESSION_SECRET": "a-long-test-session-secret",
    }, base_dir=Path(__file__).resolve().parents[1])


def _curve() -> list[dict]:
    return [{
        "time": f"{i // 12:02d}:{i % 12 * 5:02d}",
        "stress_0_10": 3.0 + (2.0 if 216 <= i < 228 else 0.0),
        "vitality_0_10": 8.0 - i / 100,
        "event_stress_input": 0.8 if 216 <= i < 228 else 0,
        "stress_interval_90_0_10": {"lower": 2.5, "upper": 4.5},
    } for i in range(288)]


def _service(database):
    settings = _settings()
    return DailyReviewService(
        DailyReviewResponseRepository(database),
        DailyReviewScheduleRepository(database),
        RetrospectiveCurveRepository(database),
        ForecastSnapshotRepository(database), ObservationRepository(database), settings,
    )


def _seed_forecast(database, person_id, target):
    return ForecastSnapshotRepository(database).save(
        person_id, target, calendar_revision="cal", semantic_revision="sem",
        algorithm_version="model-v1", forecast_version="forecast-original",
        semantic_status="complete", semantic_input=[], curve=_curve(), peaks=[],
        warning_windows=[{"risk": "unchanged"}], output={"stress_0_10": 4, "vitality_0_10": 5},
    )


def _values(**updates):
    values = {
        "start_stress": "3", "start_energy": "8", "peak_stress": "9",
        "peak_period": "evening", "end_stress": "5", "end_energy": "4",
        "energy_consumption": "7", "main_stressor": "presentation",
        "recovery_note": "walk", "free_text": "stable",
    }
    values.update(updates)
    return values


def test_daily_review_is_append_only_idempotent_and_preserves_original_forecast():
    database = memory_database()
    person = participant(database, "DR001")
    target = date(2030, 1, 15)
    original = _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedule = schedules.ensure(person.id, target, scheduled_at)
    action = {"version": "1", "schedule_id": schedule["id"], "local_date": target.isoformat()}
    service = _service(database)

    first = service.submit(
        person.id, callback_event_id="callback-1", action=action, values=_values(),
        submitted_at=datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc),
    )
    duplicate = service.submit(
        person.id, callback_event_id="callback-1", action=action, values=_values(),
        submitted_at=datetime(2030, 1, 15, 14, 31, tzinfo=timezone.utc),
    )
    revised = service.submit(
        person.id, callback_event_id="callback-2", action=action,
        values=_values(end_stress="6"),
        submitted_at=datetime(2030, 1, 15, 14, 40, tzinfo=timezone.utc),
    )

    assert first["created"] is True
    assert duplicate["created"] is False
    assert duplicate["response"]["id"] == first["response"]["id"]
    assert revised["response"]["revision"] == 2
    latest = ForecastSnapshotRepository(database).latest(person.id, target)
    assert latest["id"] == original["id"]
    assert latest["forecast_version"] == "forecast-original"
    assert latest["warning_windows"] == [{"risk": "unchanged"}]
    posterior = revised["retrospective"]
    assert posterior["source_forecast_id"] == original["id"]
    assert posterior["diagnostics"]["peak_used_as_current_state"] is False
    assert posterior["diagnostics"]["peak_anchor_reason"] == "forecast_drive_max_within_reported_period"
    assert posterior["analysis"]["forward_terminal_state"]["source"] == "daily_review_end_state"
    assert max(abs(a["stress_0_10"] - b["stress_0_10"]) for a, b in zip(_curve(), posterior["curve"])) > 0
    slopes = [
        abs(posterior["curve"][i]["stress_0_10"] - posterior["curve"][i - 1]["stress_0_10"])
        for i in range(1, len(posterior["curve"]))
    ]
    assert max(slopes) <= 0.351


def test_unknown_peak_period_never_uses_submission_time_as_a_fake_peak_anchor():
    database = memory_database()
    person = participant(database, "DR-UNKNOWN")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedule = DailyReviewScheduleRepository(database).ensure(
        person.id, target, datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    )
    result = _service(database).submit(
        person.id, callback_event_id="callback-unknown",
        action={"version": "1", "schedule_id": schedule["id"], "local_date": target.isoformat()},
        values=_values(peak_period="unknown"),
        submitted_at=datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc),
    )
    diagnostics = result["retrospective"]["diagnostics"]
    assert diagnostics["end_anchor_time"] == "22:30"
    assert diagnostics["peak_anchor_time"] != diagnostics["end_anchor_time"]


def test_daily_review_validation_and_card_contract():
    card = daily_review_card(schedule_id=str(uuid.uuid4()), local_date="2030-01-15")
    serialized = str(card)
    for field in (
        "start_stress", "start_energy", "peak_stress", "peak_period",
        "end_stress", "end_energy", "energy_consumption", "daily_review_submit",
    ):
        assert field in serialized


def test_schedule_claim_lease_retry_and_stable_message_uuid():
    database = memory_database()
    person = participant(database, "DR002")
    repo = DailyReviewScheduleRepository(database)
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedule = repo.ensure(person.id, due.date(), due)
    first = repo.claim_due(due, 120)
    assert len(first) == 1
    assert repo.claim_due(due + timedelta(seconds=60), 120) == []
    recovered = repo.claim_due(due + timedelta(seconds=121), 120)
    assert len(recovered) == 1
    assert recovered[0]["claim_token"] != first[0]["claim_token"]

    sent = []
    class Participants:
        def active_ids(self): return [person.id]
    class Bindings:
        def get_for_participant(self, _pid): return {"chat_id": "chat-1"}
    class Sender:
        def send_card(self, chat_id, card, *, message_uuid=None):
            sent.append((chat_id, card, message_uuid)); return "message-1"
    # Finish the recovered claim so the scheduler's next run sees a due retry.
    repo.mark_failed(
        schedule["id"], recovered[0]["claim_token"], now=due + timedelta(seconds=121),
        error=RuntimeError("retry"), max_attempts=5, retry_base_seconds=1,
    )
    scheduler = DailyReviewScheduler(
        schedules=repo, participants=Participants(), bindings=Bindings(), sender=Sender(),
        timezone_name="Asia/Shanghai", local_time="22:00", poll_interval_seconds=60,
        retry_base_seconds=1, max_attempts=5, claim_lease_seconds=120,
    )
    counts = asyncio.run(scheduler.run_once(due + timedelta(seconds=123)))
    assert counts["sent"] == 1
    assert sent[0][2] == schedule["id"]
    assert asyncio.run(scheduler.run_once(due + timedelta(seconds=124)))["sent"] == 0


def test_daily_review_terminal_override_changes_next_day_revision_without_peak_state():
    resolver = ForecastInitialStateResolver()
    target = date(2030, 1, 16)
    previous = {
        "id": "forecast-id", "forecast_version": "forecast-v1",
        "curve": [{"stress_0_10": 3, "vitality_0_10": 7}],
    }
    normal = resolver.resolve(target, target - timedelta(days=1), previous_day_forecast=previous)
    reviewed = resolver.resolve(
        target, target - timedelta(days=1), previous_day_forecast=previous,
        previous_day_terminal_override={
            "stress_0_10": 5, "vitality_0_10": 4,
            "retrospective_id": "retro-id", "daily_review_revision": 2,
        },
    )
    assert reviewed.mode == "previous_day_daily_review"
    assert reviewed.model_override == {"stress_0_10": 5.0, "vitality_0_10": 4.0}
    assert reviewed.revision != normal.revision


def test_environment_admin_is_superadmin_and_can_register_more_admins():
    database = memory_database()
    browser = TestClient(create_app(database, _settings()))
    login = browser.post("/admin/api/login", json={
        "username": "root-admin", "password": "correct-password",
    })
    assert login.status_code == 200
    assert login.json()["role"] == "superadmin"
    csrf = login.json()["csrf_token"]
    created = browser.post(
        "/admin/api/admin-users",
        headers={"X-CSRF-Token": csrf},
        json={"username": "second-admin", "password": "another-password", "role": "admin"},
    )
    assert created.status_code == 201
    items = browser.get("/admin/api/admin-users").json()["items"]
    root = next(item for item in items if item["username"] == "root-admin")
    assert root["role"] == "superadmin" and root["is_environment_bootstrap"] is True
    assert any(item["username"] == "second-admin" for item in items)
    viewer_created = browser.post(
        "/admin/api/admin-users",
        headers={"X-CSRF-Token": csrf},
        json={"username": "read-only", "password": "viewer-password", "role": "viewer"},
    )
    assert viewer_created.status_code == 201
    viewer = TestClient(browser.app)
    viewer_login = viewer.post("/admin/api/login", json={
        "username": "read-only", "password": "viewer-password",
    }).json()
    forbidden = viewer.post(
        "/admin/api/participants/missing/retrospective-curve/2030-01-15/rebuild",
        headers={"X-CSRF-Token": viewer_login["csrf_token"]}, json={},
    )
    assert forbidden.status_code == 403
