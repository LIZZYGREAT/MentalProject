from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import asyncio
import uuid

import pytest

from app.admin_web.auth import hash_password
from app.admin_web.main import create_app
from app.config import Settings
from app.integrations.feishu.cards import daily_review_card
from app.models import ForecastSnapshot, RetrospectiveCurveSnapshot
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
from sqlalchemy import func, select
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


def _seed_forecast(database, person_id, target, *, version="forecast-original"):
    return ForecastSnapshotRepository(database).save(
        person_id, target, calendar_revision="cal", semantic_revision="sem",
        algorithm_version="model-v1", forecast_version=version,
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


def _retrospective_count(database, participant_id) -> int:
    with database.session() as session:
        return int(session.scalar(select(func.count()).select_from(
            RetrospectiveCurveSnapshot
        ).where(
            RetrospectiveCurveSnapshot.participant_id == participant_id
        )) or 0)


def _utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _set_forecast_timeline(
    database, forecast_id: str, generated_at: datetime, *, valid: bool | None = None
) -> None:
    with database.session() as session:
        row = session.get(ForecastSnapshot, uuid.UUID(forecast_id))
        assert row is not None
        row.generated_at = generated_at
        if valid is not None:
            row.valid = valid


def _set_retrospective_generated_at(
    database, retrospective_id: str, generated_at: datetime
) -> None:
    with database.session() as session:
        row = session.get(RetrospectiveCurveSnapshot, uuid.UUID(retrospective_id))
        assert row is not None
        row.generated_at = generated_at


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
    assert first["retrospective"]["diagnostics"]["end_anchor_time"] == "22:30"
    assert (
        first["retrospective"]["diagnostics"]["end_anchor_source"]
        == "same_day_submission"
    )
    latest = ForecastSnapshotRepository(database).latest(person.id, target)
    assert latest["id"] == original["id"]
    assert latest["forecast_version"] == "forecast-original"
    assert latest["warning_windows"] == [{"risk": "unchanged"}]
    posterior = revised["retrospective"]
    assert posterior["source_forecast_id"] == original["id"]
    assert posterior["diagnostics"]["peak_used_as_current_state"] is False
    assert posterior["diagnostics"]["peak_anchor_reason"] == "forecast_drive_max_within_reported_period"
    assert posterior["diagnostics"]["end_anchor_time"] == "22:40"
    assert posterior["diagnostics"]["end_anchor_source"] == "same_day_submission"
    assert posterior["diagnostics"]["review_local_date"] == "2030-01-15"
    assert posterior["diagnostics"]["submitted_local_date"] == "2030-01-15"
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
    assert "当天收尾压力" in serialized
    assert "当天收尾精力" in serialized
    assert "当前/收尾" not in serialized


def test_cross_midnight_catch_up_uses_scheduled_closing_anchor_and_real_submit_time():
    database = memory_database()
    person = participant(database, "DR-CROSS-MIDNIGHT")
    target = date(2030, 1, 15)
    original = _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    sent = []

    class Participants:
        def active_ids(self):
            return [person.id]

    class Bindings:
        def get_for_participant(self, _pid):
            return {"chat_id": "chat-1"}

    class Sender:
        def send_card(self, chat_id, card, *, message_uuid=None):
            sent.append((chat_id, card, message_uuid))
            return "message-1"

    scheduler = DailyReviewScheduler(
        schedules=schedules,
        participants=Participants(),
        bindings=Bindings(),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        catch_up_minutes=120,
        validity_minutes=1440,
    )
    restart = datetime(2030, 1, 15, 16, 5, tzinfo=timezone.utc)
    counts = asyncio.run(scheduler.run_once(restart))
    action = sent[0][1]["elements"][1]["elements"][-1]["value"]

    service = _service(database)
    result = service.submit(
        person.id,
        callback_event_id="callback-cross-midnight",
        action=action,
        values=_values(end_stress="9", end_energy="2"),
        submitted_at=datetime(2030, 1, 15, 16, 6, tzinfo=timezone.utc),
    )
    response = result["response"]
    retrospective = result["retrospective"]
    diagnostics = retrospective["diagnostics"]

    assert counts == {
        "ensured": 1, "sent": 1, "unavailable": 0, "failed": 0,
    }
    assert response["local_date"] == "2030-01-15"
    assert response["submitted_at"].startswith("2030-01-15T16:06:00")
    assert diagnostics["review_local_date"] == "2030-01-15"
    assert diagnostics["submitted_local_date"] == "2030-01-16"
    assert diagnostics["end_anchor_time"] == "22:00"
    assert diagnostics["end_anchor_time"] not in {"00:05", "00:06"}
    assert diagnostics["end_anchor_source"] == "scheduled_review_time"
    assert retrospective["source_forecast_id"] == original["id"]
    assert diagnostics["original_forecast_immutable"] is True

    _, correct_analysis, _ = service.reconstructor.reconstruct(
        _curve(), response,
        end_anchor_minute=22 * 60,
        end_anchor_source="scheduled_review_time",
        review_local_date="2030-01-15",
        submitted_local_date="2030-01-16",
    )
    _, midnight_analysis, _ = service.reconstructor.reconstruct(
        _curve(), response,
        end_anchor_minute=5,
        end_anchor_source="scheduled_review_time",
        review_local_date="2030-01-15",
        submitted_local_date="2030-01-16",
    )
    forward = retrospective["analysis"]["forward_terminal_state"]
    assert forward == correct_analysis["forward_terminal_state"]
    assert forward != midnight_analysis["forward_terminal_state"]

    next_day = ForecastInitialStateResolver().resolve(
        target + timedelta(days=1),
        target,
        previous_day_forecast=original,
        previous_day_terminal_override={
            **forward,
            "retrospective_id": retrospective["id"],
            "daily_review_revision": response["revision"],
        },
    )
    assert next_day.mode == "previous_day_daily_review"
    assert next_day.model_override == {
        "stress_0_10": forward["stress_0_10"],
        "vitality_0_10": forward["vitality_0_10"],
    }
    rebuilt = service.rebuild(person.id, target)
    assert rebuilt["diagnostics"]["end_anchor_time"] == "22:00"
    assert rebuilt["diagnostics"]["end_anchor_source"] == "scheduled_review_time"


def test_next_morning_recovered_card_still_uses_previous_day_closing_anchor():
    database = memory_database()
    person = participant(database, "DR-LATE-RECOVERY-ANCHOR")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    due = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    schedule = schedules.ensure(person.id, target, due)
    claimed = schedules.claim_due(due, 120)[0]
    schedules.mark_unavailable(
        schedule["id"], claimed["claim_token"], now=due
    )
    sent = []

    class Participants:
        def active_ids(self):
            return [person.id]

    class Bindings:
        def get_for_participant(self, _pid):
            return {"chat_id": "recovered-chat"}

    class Sender:
        def send_card(self, chat_id, card, *, message_uuid=None):
            sent.append((chat_id, card, message_uuid))
            return "recovered-message"

    scheduler = DailyReviewScheduler(
        schedules=schedules,
        participants=Participants(),
        bindings=Bindings(),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        catch_up_minutes=120,
        validity_minutes=1440,
    )
    recovered_at = datetime(2030, 1, 16, 2, 0, tzinfo=timezone.utc)
    assert asyncio.run(scheduler.run_once(recovered_at))["sent"] == 1
    action = sent[0][1]["elements"][1]["elements"][-1]["value"]
    result = _service(database).submit(
        person.id,
        callback_event_id="callback-late-recovery",
        action=action,
        values=_values(),
        submitted_at=datetime(2030, 1, 16, 2, 5, tzinfo=timezone.utc),
    )

    diagnostics = result["retrospective"]["diagnostics"]
    assert result["response"]["submitted_at"].startswith("2030-01-16T02:05:00")
    assert diagnostics["submitted_local_date"] == "2030-01-16"
    assert diagnostics["end_anchor_time"] == "22:00"
    assert diagnostics["end_anchor_time"] != "10:05"
    assert diagnostics["end_anchor_source"] == "scheduled_review_time"


def test_schedule_date_mismatch_fails_closed_before_response_is_saved():
    database = memory_database()
    person = participant(database, "DR-BAD-SCHEDULE-DATE")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    schedule = schedules.ensure(
        person.id,
        target,
        # This is Jan 16 at 22:00 in the configured business timezone.
        datetime(2030, 1, 16, 14, 0, tzinfo=timezone.utc),
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }

    with pytest.raises(
        ValueError, match="scheduled time does not match local date"
    ):
        _service(database).submit(
            person.id,
            callback_event_id="callback-bad-schedule-date",
            action=action,
            values=_values(),
            submitted_at=datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc),
        )
    assert DailyReviewResponseRepository(database).latest(person.id, target) is None


def test_expired_sent_card_cannot_save_response_or_retrospective():
    database = memory_database()
    person = participant(database, "DR-EXPIRED-CALLBACK")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    schedule = schedules.ensure(person.id, target, scheduled_at)
    claim = schedules.claim_due(scheduled_at, 120)[0]
    assert schedules.mark_sent(
        schedule["id"], claim["claim_token"],
        now=scheduled_at, provider_message_id="sent-card",
    ) is True
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }

    # Jan 16 at 23:00 Asia/Shanghai is one hour after default validity.
    with pytest.raises(ValueError, match="submission window has expired"):
        _service(database).submit(
            person.id,
            callback_event_id="callback-expired-card",
            action=action,
            values=_values(),
            submitted_at=datetime(2030, 1, 16, 15, 0, tzinfo=timezone.utc),
        )

    assert DailyReviewResponseRepository(database).latest(person.id, target) is None
    assert RetrospectiveCurveRepository(database).latest(person.id, target) is None


@pytest.mark.parametrize("boundary_minutes", [0, 24 * 60])
def test_submission_window_includes_both_scheduled_and_valid_until_boundaries(
    boundary_minutes,
):
    database = memory_database()
    person = participant(database, f"DR-VALID-BOUNDARY-{boundary_minutes}")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    valid_until = scheduled_at + timedelta(days=1)
    schedule = schedules.ensure(
        person.id, target, scheduled_at, valid_until=valid_until
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }

    result = _service(database).submit(
        person.id,
        callback_event_id=f"callback-boundary-{boundary_minutes}",
        action=action,
        values=_values(),
        submitted_at=scheduled_at + timedelta(minutes=boundary_minutes),
    )

    assert result["created"] is True
    assert result["retrospective"]["daily_review_response_id"] == result["response"]["id"]


def test_submission_before_scheduled_time_has_no_model_side_effects():
    database = memory_database()
    person = participant(database, "DR-BEFORE-SCHEDULE")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    schedule = schedules.ensure(person.id, target, scheduled_at)
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }

    with pytest.raises(ValueError, match="before its scheduled time"):
        _service(database).submit(
            person.id,
            callback_event_id="callback-too-early",
            action=action,
            values=_values(),
            submitted_at=scheduled_at - timedelta(minutes=1),
        )

    assert DailyReviewResponseRepository(database).latest(person.id, target) is None
    assert RetrospectiveCurveRepository(database).latest(person.id, target) is None


def test_duplicate_callback_does_not_rebuild_against_a_new_forecast_version():
    database = memory_database()
    person = participant(database, "DR-IDEMPOTENT-FORECAST")
    target = date(2030, 1, 15)
    original = _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    schedule = schedules.ensure(person.id, target, scheduled_at)
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    first = service.submit(
        person.id,
        callback_event_id="callback-idempotent",
        action=action,
        values=_values(),
        submitted_at=scheduled_at + timedelta(minutes=30),
    )
    assert _retrospective_count(database, person.id) == 1

    latest_forecast = _seed_forecast(
        database, person.id, target, version="forecast-v2"
    )
    assert latest_forecast["id"] != original["id"]
    duplicate = service.submit(
        person.id,
        callback_event_id="callback-idempotent",
        action=action,
        values=_values(),
        submitted_at=scheduled_at + timedelta(minutes=35),
    )

    assert duplicate["created"] is False
    assert duplicate["response"]["id"] == first["response"]["id"]
    assert duplicate["retrospective"]["id"] == first["retrospective"]["id"]
    assert duplicate["retrospective"]["source_forecast_version"] == "forecast-original"
    assert _retrospective_count(database, person.id) == 1


def test_duplicate_callback_recovers_once_when_response_exists_without_retrospective():
    database = memory_database()
    person = participant(database, "DR-IDEMPOTENT-RECOVERY")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    schedule = schedules.ensure(person.id, target, scheduled_at)
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    rebuild = service.rebuild

    def crash_after_response(*_args, **_kwargs):
        raise RuntimeError("simulated reconstruction crash")

    service.rebuild = crash_after_response
    with pytest.raises(RuntimeError, match="simulated reconstruction crash"):
        service.submit(
            person.id,
            callback_event_id="callback-recovery",
            action=action,
            values=_values(),
            submitted_at=scheduled_at + timedelta(minutes=30),
        )
    response = DailyReviewResponseRepository(database).latest(person.id, target)
    assert response is not None
    assert RetrospectiveCurveRepository(database).latest(person.id, target) is None

    service.rebuild = rebuild
    recovered = service.submit(
        person.id,
        callback_event_id="callback-recovery",
        action=action,
        values=_values(),
        submitted_at=scheduled_at + timedelta(minutes=31),
    )
    repeated = service.submit(
        person.id,
        callback_event_id="callback-recovery",
        action=action,
        values=_values(),
        submitted_at=scheduled_at + timedelta(minutes=32),
    )

    assert recovered["created"] is False
    assert recovered["response"]["id"] == response["id"]
    assert repeated["retrospective"]["id"] == recovered["retrospective"]["id"]
    assert _retrospective_count(database, person.id) == 1


def test_crash_recovery_uses_forecast_visible_at_original_submission():
    database = memory_database()
    person = participant(database, "DR-CAUSAL-FORECAST")
    target = date(2030, 1, 15)
    t0 = datetime(2030, 1, 15, 14, 10, tzinfo=timezone.utc)
    t1 = datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    t2 = datetime(2030, 1, 15, 14, 40, tzinfo=timezone.utc)
    forecast_v1 = _seed_forecast(
        database, person.id, target, version="forecast-causal-v1"
    )
    _set_forecast_timeline(database, forecast_v1["id"], t0)
    schedule = DailyReviewScheduleRepository(database).ensure(
        person.id, target, datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    rebuild = service.rebuild

    def crash_after_response(*_args, **_kwargs):
        raise RuntimeError("simulated causal forecast crash")

    service.rebuild = crash_after_response
    with pytest.raises(RuntimeError, match="causal forecast crash"):
        service.submit(
            person.id, callback_event_id="callback-causal-forecast",
            action=action, values=_values(), submitted_at=t1,
        )

    forecast_v2 = _seed_forecast(
        database, person.id, target, version="forecast-causal-v2"
    )
    _set_forecast_timeline(database, forecast_v2["id"], t2)
    with database.session() as session:
        stored_v1 = session.get(ForecastSnapshot, uuid.UUID(forecast_v1["id"]))
        assert stored_v1 is not None and stored_v1.valid is False

    service.rebuild = rebuild
    recovered = service.submit(
        person.id, callback_event_id="callback-causal-forecast",
        action=action, values={}, submitted_at=t2 + timedelta(minutes=1),
    )

    assert recovered["created"] is False
    assert recovered["retrospective"]["source_forecast_version"] == "forecast-causal-v1"
    assert recovered["retrospective"]["source_forecast_id"] == forecast_v1["id"]


def test_crash_recovery_excludes_observations_after_original_submission():
    database = memory_database()
    person = participant(database, "DR-CAUSAL-OBSERVATION")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    t1 = datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    observations = ObservationRepository(database)
    observation_o1 = observations.add(
        person.id, "check_in", {"stress_0_10": 4},
        observed_at=t1 - timedelta(minutes=20),
    )
    schedule = DailyReviewScheduleRepository(database).ensure(
        person.id, target, datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    rebuild = service.rebuild

    def crash_after_response(*_args, **_kwargs):
        raise RuntimeError("simulated causal observation crash")

    service.rebuild = crash_after_response
    with pytest.raises(RuntimeError, match="causal observation crash"):
        service.submit(
            person.id, callback_event_id="callback-causal-observation",
            action=action, values=_values(), submitted_at=t1,
        )

    observation_o2 = observations.add(
        person.id, "check_in", {"stress_0_10": 9},
        observed_at=t1 + timedelta(minutes=1),
    )
    service.rebuild = rebuild
    recovered = service.submit(
        person.id, callback_event_id="callback-causal-observation",
        action=action, values={}, submitted_at=t1 + timedelta(minutes=2),
    )
    used = recovered["retrospective"]["diagnostics"][
        "observation_smoothing"
    ]["observation_ids"]

    assert str(observation_o1) in used
    assert str(observation_o2) not in used


def test_crash_recovery_matches_non_crash_causal_sources():
    database = memory_database()
    normal_person = participant(database, "DR-CAUSAL-NORMAL")
    recovery_person = participant(database, "DR-CAUSAL-RECOVERY")
    target = date(2030, 1, 15)
    t0 = datetime(2030, 1, 15, 14, 10, tzinfo=timezone.utc)
    t1 = datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    t2 = datetime(2030, 1, 15, 14, 40, tzinfo=timezone.utc)
    schedules = DailyReviewScheduleRepository(database)

    cases = []
    for person, callback in (
        (normal_person, "callback-causal-normal"),
        (recovery_person, "callback-causal-recovery"),
    ):
        forecast_v1 = _seed_forecast(
            database, person.id, target, version="forecast-shared-v1"
        )
        _set_forecast_timeline(database, forecast_v1["id"], t0)
        schedule = schedules.ensure(
            person.id, target, datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
        )
        cases.append((person, callback, {
            "version": "1", "schedule_id": schedule["id"],
            "local_date": target.isoformat(),
        }))

    normal_service = _service(database)
    normal = normal_service.submit(
        normal_person.id, callback_event_id=cases[0][1],
        action=cases[0][2], values=_values(), submitted_at=t1,
    )

    recovery_service = _service(database)
    rebuild = recovery_service.rebuild

    def crash_after_response(*_args, **_kwargs):
        raise RuntimeError("simulated comparison crash")

    recovery_service.rebuild = crash_after_response
    with pytest.raises(RuntimeError, match="comparison crash"):
        recovery_service.submit(
            recovery_person.id, callback_event_id=cases[1][1],
            action=cases[1][2], values=_values(), submitted_at=t1,
        )

    for person in (normal_person, recovery_person):
        forecast_v2 = _seed_forecast(
            database, person.id, target, version="forecast-shared-v2"
        )
        _set_forecast_timeline(database, forecast_v2["id"], t2)

    recovery_service.rebuild = rebuild
    recovered = recovery_service.submit(
        recovery_person.id, callback_event_id=cases[1][1],
        action=cases[1][2], values={}, submitted_at=t2 + timedelta(minutes=1),
    )

    assert normal["retrospective"]["source_forecast_version"] == "forecast-shared-v1"
    assert recovered["retrospective"]["source_forecast_version"] == "forecast-shared-v1"
    assert (
        normal["retrospective"]["observation_revision"]
        == recovered["retrospective"]["observation_revision"]
    )


def test_late_recovery_of_old_revision_does_not_replace_new_revision():
    database = memory_database()
    person = participant(database, "DR-REVISION-RECOVERY")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedule = DailyReviewScheduleRepository(database).ensure(
        person.id, target, scheduled_at
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    rebuild = service.rebuild

    def crash_after_response(*_args, **_kwargs):
        raise RuntimeError("simulated revision one crash")

    service.rebuild = crash_after_response
    with pytest.raises(RuntimeError, match="revision one crash"):
        service.submit(
            person.id, callback_event_id="callback-revision-1",
            action=action, values=_values(end_stress="4"),
            submitted_at=scheduled_at + timedelta(minutes=20),
        )

    service.rebuild = rebuild
    revision_two = service.submit(
        person.id, callback_event_id="callback-revision-2",
        action=action, values=_values(end_stress="7"),
        submitted_at=scheduled_at + timedelta(minutes=25),
    )
    recovered_revision_one = service.submit(
        person.id, callback_event_id="callback-revision-1",
        action=action, values={},
        submitted_at=scheduled_at + timedelta(minutes=30),
    )
    _set_retrospective_generated_at(
        database, revision_two["retrospective"]["id"],
        scheduled_at + timedelta(hours=1),
    )
    _set_retrospective_generated_at(
        database, recovered_revision_one["retrospective"]["id"],
        scheduled_at + timedelta(hours=2),
    )

    latest = RetrospectiveCurveRepository(database).latest(person.id, target)
    assert revision_two["response"]["revision"] == 2
    assert recovered_revision_one["retrospective"]["daily_review_revision"] == 1
    assert latest["daily_review_revision"] == 2
    assert latest["id"] == revision_two["retrospective"]["id"]


def test_latest_retrospective_uses_newest_rebuild_within_same_revision():
    database = memory_database()
    person = participant(database, "DR-SAME-REVISION-REBUILD")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target, version="forecast-admin-v1")
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedule = DailyReviewScheduleRepository(database).ensure(
        person.id, target, scheduled_at
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    service.submit(
        person.id, callback_event_id="callback-admin-revision-1",
        action=action, values=_values(),
        submitted_at=scheduled_at + timedelta(minutes=20),
    )
    revision_two = service.submit(
        person.id, callback_event_id="callback-admin-revision-2",
        action=action, values=_values(end_stress="7"),
        submitted_at=scheduled_at + timedelta(minutes=25),
    )
    _seed_forecast(database, person.id, target, version="forecast-admin-v2")
    rebuilt = service.rebuild(person.id, target)
    _set_retrospective_generated_at(
        database, revision_two["retrospective"]["id"],
        scheduled_at + timedelta(hours=1),
    )
    _set_retrospective_generated_at(
        database, rebuilt["id"], scheduled_at + timedelta(hours=2),
    )

    latest = RetrospectiveCurveRepository(database).latest(person.id, target)
    assert revision_two["retrospective"]["daily_review_revision"] == 2
    assert rebuilt["daily_review_revision"] == 2
    assert rebuilt["id"] != revision_two["retrospective"]["id"]
    assert rebuilt["source_forecast_version"] == "forecast-admin-v2"
    assert latest["id"] == rebuilt["id"]


def test_successful_callback_retry_after_expiry_returns_original_result():
    database = memory_database()
    person = participant(database, "DR-EXPIRED-RETRY-SUCCESS")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    valid_until = scheduled_at + timedelta(days=1)
    schedule = schedules.ensure(
        person.id, target, scheduled_at, valid_until=valid_until
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    first = service.submit(
        person.id,
        callback_event_id="callback-expired-retry-success",
        action=action,
        values=_values(),
        submitted_at=valid_until - timedelta(seconds=1),
    )
    retry = service.submit(
        person.id,
        callback_event_id="callback-expired-retry-success",
        action=action,
        values={"this": "retry payload must not be normalized"},
        submitted_at=valid_until + timedelta(seconds=1),
    )

    assert retry["created"] is False
    assert retry["response"]["id"] == first["response"]["id"]
    assert _utc_timestamp(retry["response"]["submitted_at"]) == _utc_timestamp(
        first["response"]["submitted_at"]
    )
    assert retry["retrospective"]["id"] == first["retrospective"]["id"]
    assert _retrospective_count(database, person.id) == 1


def test_reconstruction_crash_recovers_from_same_callback_after_expiry():
    database = memory_database()
    person = participant(database, "DR-EXPIRED-RETRY-RECOVERY")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    valid_until = scheduled_at + timedelta(days=1)
    schedule = schedules.ensure(
        person.id, target, scheduled_at, valid_until=valid_until
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    rebuild = service.rebuild

    def crash_after_response(*_args, **_kwargs):
        raise RuntimeError("simulated reconstruction crash at expiry")

    service.rebuild = crash_after_response
    with pytest.raises(RuntimeError, match="crash at expiry"):
        service.submit(
            person.id,
            callback_event_id="callback-expired-retry-recovery",
            action=action,
            values=_values(),
            submitted_at=valid_until - timedelta(seconds=1),
        )
    response = DailyReviewResponseRepository(database).latest(person.id, target)
    assert response is not None
    assert _retrospective_count(database, person.id) == 0

    service.rebuild = rebuild
    recovered = service.submit(
        person.id,
        callback_event_id="callback-expired-retry-recovery",
        action=action,
        values={"invalid": "ignored for an accepted callback"},
        submitted_at=valid_until + timedelta(seconds=1),
    )
    repeated = service.submit(
        person.id,
        callback_event_id="callback-expired-retry-recovery",
        action=action,
        values={},
        submitted_at=valid_until + timedelta(minutes=5),
    )

    assert recovered["created"] is False
    assert recovered["response"]["id"] == response["id"]
    assert _utc_timestamp(recovered["response"]["submitted_at"]) == _utc_timestamp(
        response["submitted_at"]
    )
    assert repeated["retrospective"]["id"] == recovered["retrospective"]["id"]
    assert _retrospective_count(database, person.id) == 1


def test_expiry_rejection_rechecks_callback_for_boundary_race():
    database = memory_database()
    person = participant(database, "DR-EXPIRY-RACE")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    schedules = DailyReviewScheduleRepository(database)
    scheduled_at = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
    valid_until = scheduled_at + timedelta(days=1)
    schedule = schedules.ensure(
        person.id, target, scheduled_at, valid_until=valid_until
    )
    action = {
        "version": "1", "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }
    service = _service(database)
    first = service.submit(
        person.id,
        callback_event_id="callback-expiry-race",
        action=action,
        values=_values(),
        submitted_at=valid_until - timedelta(seconds=1),
    )
    lookup = service.responses.get_by_callback_event_id
    calls = 0

    def miss_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return lookup(*args, **kwargs)

    service.responses.get_by_callback_event_id = miss_once
    retry = service.submit(
        person.id,
        callback_event_id="callback-expiry-race",
        action=action,
        values=_values(),
        submitted_at=valid_until + timedelta(seconds=1),
    )

    assert calls == 2
    assert retry["created"] is False
    assert retry["retrospective"]["id"] == first["retrospective"]["id"]
    assert _retrospective_count(database, person.id) == 1


def test_duplicate_callback_identity_mismatch_is_rejected():
    database = memory_database()
    person = participant(database, "DR-CALLBACK-IDENTITY")
    first_date = date(2030, 1, 15)
    second_date = date(2030, 1, 16)
    _seed_forecast(database, person.id, first_date)
    schedules = DailyReviewScheduleRepository(database)
    first_schedule = schedules.ensure(
        person.id,
        first_date,
        datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc),
    )
    second_schedule = schedules.ensure(
        person.id,
        second_date,
        datetime(2030, 1, 16, 14, 0, tzinfo=timezone.utc),
    )
    service = _service(database)
    first = service.submit(
        person.id,
        callback_event_id="callback-identity-collision",
        action={
            "version": "1", "schedule_id": first_schedule["id"],
            "local_date": first_date.isoformat(),
        },
        values=_values(),
        submitted_at=datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="identity does not match"):
        service.submit(
            person.id,
            callback_event_id="callback-identity-collision",
            action={
                "version": "1", "schedule_id": second_schedule["id"],
                "local_date": second_date.isoformat(),
            },
            values={},
            submitted_at=datetime(2030, 1, 16, 14, 0, tzinfo=timezone.utc),
        )

    assert DailyReviewResponseRepository(database).latest(
        person.id, second_date
    ) is None
    assert RetrospectiveCurveRepository(database).latest(
        person.id, second_date
    ) is None
    assert _retrospective_count(database, person.id) == 1
    assert first["response"]["local_date"] == first_date.isoformat()


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


def test_old_unavailable_cards_expire_instead_of_batch_sending():
    database = memory_database()
    person = participant(database, "DR-EXPIRY")
    repo = DailyReviewScheduleRepository(database)
    old_schedules = []
    for day_number in (10, 11, 12, 13, 14):
        local_day = date(2030, 1, day_number)
        due = datetime(2030, 1, day_number, 14, tzinfo=timezone.utc)
        schedule = repo.ensure(person.id, local_day, due)
        claimed = repo.claim_due(due, 120)
        assert len(claimed) == 1
        repo.mark_unavailable(
            schedule["id"], claimed[0]["claim_token"], now=due
        )
        old_schedules.append(schedule)

    sent = []

    class Participants:
        def active_ids(self):
            return [person.id]

    class Bindings:
        def get_for_participant(self, _pid):
            return {"chat_id": "chat-available"}

    class Sender:
        def send_card(self, chat_id, card, *, message_uuid=None):
            sent.append((chat_id, card, message_uuid))
            return "message-current"

    scheduler = DailyReviewScheduler(
        schedules=repo,
        participants=Participants(),
        bindings=Bindings(),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
    )
    now = datetime(2030, 1, 15, 14, 5, tzinfo=timezone.utc)
    counts = asyncio.run(scheduler.run_once(now))

    assert counts["sent"] == 1
    assert len(sent) == 1
    assert "2030-01-15" in str(sent[0][1])
    assert all(repo.get(item["id"])["status"] == "expired" for item in old_schedules)


def test_restart_shortly_after_midnight_catches_up_previous_day_once():
    database = memory_database()
    person = participant(database, "DR-CATCHUP")
    repo = DailyReviewScheduleRepository(database)
    sent = []

    class Participants:
        def active_ids(self):
            return [person.id]

    class Bindings:
        def get_for_participant(self, _pid):
            return {"chat_id": "chat-1"}

    class Sender:
        def send_card(self, chat_id, card, *, message_uuid=None):
            sent.append((chat_id, card, message_uuid))
            return "message-1"

    scheduler = DailyReviewScheduler(
        schedules=repo,
        participants=Participants(),
        bindings=Bindings(),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        catch_up_minutes=120,
        validity_minutes=1440,
    )
    # 2030-01-16 00:05 Asia/Shanghai: the process missed Jan 15 at 22:00.
    restart = datetime(2030, 1, 15, 16, 5, tzinfo=timezone.utc)
    first = asyncio.run(scheduler.run_once(restart))
    second = asyncio.run(scheduler.run_once(restart + timedelta(minutes=1)))

    assert first["ensured"] == 1
    assert first["sent"] == 1
    assert second["sent"] == 0
    assert len(sent) == 1
    assert "2030-01-15" in str(sent[0][1])


def test_yesterday_unavailable_card_sends_when_binding_recovers_within_validity():
    database = memory_database()
    person = participant(database, "DR-RECOVER-VALID")
    repo = DailyReviewScheduleRepository(database)
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedule = repo.ensure(person.id, date(2030, 1, 15), due)
    claimed = repo.claim_due(due, 120)[0]
    repo.mark_unavailable(
        schedule["id"], claimed["claim_token"], now=due
    )
    sent = []

    class Participants:
        def active_ids(self):
            return [person.id]

    class Bindings:
        def get_for_participant(self, _pid):
            return {"chat_id": "restored-chat"}

    class Sender:
        def send_card(self, chat_id, card, *, message_uuid=None):
            sent.append((chat_id, card, message_uuid))
            return "restored-message"

    scheduler = DailyReviewScheduler(
        schedules=repo,
        participants=Participants(),
        bindings=Bindings(),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        catch_up_minutes=120,
        validity_minutes=1440,
    )
    # 10:00 the following local day is outside creation catch-up but still
    # inside the existing card's 24-hour delivery window.
    recovered_at = datetime(2030, 1, 16, 2, 0, tzinfo=timezone.utc)
    counts = asyncio.run(scheduler.run_once(recovered_at))

    assert counts["ensured"] == 0
    assert counts["sent"] == 1
    assert len(sent) == 1
    assert repo.get(schedule["id"])["status"] == "sent"


def test_restart_after_catch_up_window_does_not_create_old_schedule():
    database = memory_database()
    person = participant(database, "DR-NO-LATE-CATCHUP")
    repo = DailyReviewScheduleRepository(database)

    class Participants:
        def active_ids(self):
            return [person.id]

    class Bindings:
        def get_for_participant(self, _pid):
            return {"chat_id": "chat-1"}

    class Sender:
        def send_card(self, *_args, **_kwargs):
            raise AssertionError("no stale card should be sent")

    scheduler = DailyReviewScheduler(
        schedules=repo,
        participants=Participants(),
        bindings=Bindings(),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        catch_up_minutes=120,
        validity_minutes=1440,
    )
    # 03:00 local time is outside the bounded two-hour catch-up window.
    restart = datetime(2030, 1, 15, 19, 0, tzinfo=timezone.utc)
    counts = asyncio.run(scheduler.run_once(restart))

    assert counts == {
        "ensured": 0,
        "sent": 0,
        "unavailable": 0,
        "failed": 0,
    }


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
