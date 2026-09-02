from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import asyncio
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.admin_web.auth import hash_password
from app.admin_web.main import create_app
from app.config import Settings
from app.integrations.feishu.card_callback import FeishuCardCallbackServer
from app.integrations.feishu.cards import daily_review_card
from app.models import (
    ForecastCurrentnessEvent,
    ForecastSnapshot,
    Participant,
    RetrospectiveCurveSnapshot,
)
from app.repositories import (
    ForecastSnapshotRepository,
    ObservationRepository,
    ParticipantRepository,
)
from app.repositories_daily_review import (
    DailyReviewResponseRepository,
    DailyReviewScheduleRepository,
    RetrospectiveCurveRepository,
)
from app.services.daily_review_scheduler import DailyReviewScheduler
from app.services.daily_review_service import DailyReviewService
from app.services.card_action_service import CardActionService
from app.services.curve_analysis import analyze_curve
from app.services.forecast_initial_state import ForecastInitialStateResolver
from app.services.pressure_curve_service import PressureCurveView
from helpers import memory_database, participant
from sqlalchemy import func, select
from starlette.testclient import TestClient


def _settings() -> Settings:
    return Settings.from_env({
        "APP_ENV": "test", "FEISHU_BOT_APP_ID": "app", "FEISHU_BOT_APP_SECRET": "secret",
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


def _seed_forecast(
    database,
    person_id,
    target,
    *,
    version="forecast-original",
    curve=None,
):
    return ForecastSnapshotRepository(database).save(
        person_id, target, calendar_revision="cal", semantic_revision="sem",
        algorithm_version="model-v1", forecast_version=version,
        semantic_status="complete", semantic_input=[], curve=curve or _curve(), peaks=[],
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
        events = session.execute(select(ForecastCurrentnessEvent).where(
            ForecastCurrentnessEvent.forecast_id == row.id,
        )).scalars().all()
        transition_times = {event.occurred_at for event in events}
        transition_events = session.execute(select(ForecastCurrentnessEvent).where(
            ForecastCurrentnessEvent.participant_id == row.participant_id,
            ForecastCurrentnessEvent.local_date == row.local_date,
            ForecastCurrentnessEvent.occurred_at.in_(transition_times),
        )).scalars().all()
        for event in transition_events:
            event.occurred_at = generated_at
        if valid is not None:
            row.valid = valid


def _set_latest_currentness_event(
    database, forecast_id: str, occurred_at: datetime
) -> None:
    with database.session() as session:
        event = session.execute(
            select(ForecastCurrentnessEvent).where(
                ForecastCurrentnessEvent.forecast_id == uuid.UUID(forecast_id)
            ).order_by(ForecastCurrentnessEvent.id.desc()).limit(1)
        ).scalar_one()
        event.occurred_at = occurred_at


def _set_retrospective_generated_at(
    database, retrospective_id: str, generated_at: datetime
) -> None:
    with database.session() as session:
        row = session.get(RetrospectiveCurveSnapshot, uuid.UUID(retrospective_id))
        assert row is not None
        row.generated_at = generated_at


def _daily_review_action(
    database, participant_id: uuid.UUID, target: date, scheduled_at: datetime
) -> dict[str, str]:
    schedule = DailyReviewScheduleRepository(database).ensure(
        participant_id, target, scheduled_at
    )
    return {
        "version": "1",
        "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
    }


def _daily_review_scheduler(database, participant_id: uuid.UUID, sent: list):
    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "chat-1"}

    class Sender:
        def send_card(self, chat_id, card, *, message_uuid=None):
            sent.append((chat_id, card, message_uuid))
            return "message-1"

    return DailyReviewScheduler(
        schedules=DailyReviewScheduleRepository(database),
        participants=ParticipantRepository(database),
        bindings=Bindings(),
        forecasts=ForecastSnapshotRepository(database),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        retry_base_seconds=60,
    )


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
    schedule_id = str(uuid.uuid4())
    card = daily_review_card(schedule_id=schedule_id, local_date="2030-01-15")
    assert card["schema"] == "2.0"
    assert "elements" not in card
    assert "body" in card
    form = next(
        element
        for element in card["body"]["elements"]
        if element["tag"] == "form"
    )
    assert form["name"] == "mindflow_daily_review"
    form_elements = form["elements"]
    component_names = [
        element["name"] for element in form_elements if element.get("name")
    ]
    assert len(component_names) == len(set(component_names))
    assert set(component_names) == {
        "start_stress",
        "start_energy",
        "peak_stress",
        "peak_period",
        "end_stress",
        "end_energy",
        "energy_consumption",
        "main_stressor",
        "recovery_note",
        "free_text",
        "daily_review_submit",
    }
    fields = {element.get("name"): element for element in form_elements}
    prompts = {
        "start_stress": "① 回顾 2030-01-15：当天早晨刚开始一天时，你的压力有多高？",
        "start_energy": "② 回顾 2030-01-15：当天早晨的精力怎么样？",
        "peak_stress": "③ 回顾 2030-01-15：当天最高压力大约有多高？",
        "peak_period": "④ 回顾 2030-01-15：当天最高压力大约出现在什么时候？",
        "end_stress": "⑤ 回顾 2030-01-15：当天结束时（约晚间/睡前），你的压力有多高？",
        "end_energy": "⑥ 回顾 2030-01-15：当天结束时，你还剩多少精力？",
        "energy_consumption": "⑦ 回顾 2030-01-15：当天整体让你感觉被消耗了多少？（选填）",
    }

    for name, prompt in prompts.items():
        field_index = form_elements.index(fields[name])
        visible_description = form_elements[field_index - 1]
        assert visible_description["tag"] == "markdown"
        assert prompt in visible_description["content"]

    required_fields = {
        "start_stress",
        "start_energy",
        "peak_stress",
        "peak_period",
        "end_stress",
        "end_energy",
    }
    optional_fields = {
        "energy_consumption",
        "main_stressor",
        "recovery_note",
        "free_text",
    }
    assert all(fields[name]["required"] is True for name in required_fields)
    assert all(fields[name]["required"] is False for name in optional_fields)
    review_selects = [
        element for element in form_elements if element["tag"] == "select_static"
    ]
    assert review_selects
    assert all("label" not in select for select in review_selects)
    assert [option["value"] for option in fields["start_stress"]["options"]] == [
        str(value) for value in range(11)
    ]
    assert [option["value"] for option in fields["peak_period"]["options"]] == [
        "overnight",
        "early_morning",
        "morning",
        "noon",
        "afternoon",
        "evening",
        "late_night",
        "unknown",
    ]
    assert fields["energy_consumption"]["required"] is False
    assert fields["main_stressor"]["max_length"] == 300
    assert fields["recovery_note"]["max_length"] == 300
    assert fields["free_text"]["max_length"] == 1000
    assert fields["free_text"]["input_type"] == "multiline_text"
    assert fields["free_text"]["rows"] == 3
    button = fields["daily_review_submit"]
    assert button["form_action_type"] == "submit"
    assert "action_type" not in button
    assert button["behaviors"] == [{
        "type": "callback",
        "value": {
            "mindflow_action": "daily_review_submit",
            "version": "1",
            "schedule_id": schedule_id,
            "local_date": "2030-01-15",
            "card_version": "daily-review-v1",
        },
    }]
    serialized = str(card)
    assert "0 = 完全没有压力" in serialized
    assert "10 = 已经非常难承受" in serialized
    assert "0 = 几乎没有精力" in serialized
    assert "10 = 精力非常充足" in serialized
    assert "第 ⑤、⑥ 项会用于帮助估计下一天的起始状态" in serialized
    assert "当前主要用于研究分析，不会直接改变压力或精力曲线" in serialized
    assert "以上文字主要用于回顾和研究分析，目前不会直接改变压力曲线数值" in serialized
    assert "如果这是次日补填" in serialized
    assert "不要填写此刻状态" in serialized
    assert "现在/今天结束时" not in serialized


def test_daily_review_p2_callback_persists_form_values_and_retrospective():
    database = memory_database()
    person = participant(database, "DR-P2-CALLBACK")
    now = datetime.now(timezone.utc)
    target = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    original = _seed_forecast(database, person.id, target)
    schedule = DailyReviewScheduleRepository(database).ensure(
        person.id,
        target,
        now - timedelta(minutes=1),
    )
    action_value = {
        "mindflow_action": "daily_review_submit",
        "version": "1",
        "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
        "card_version": schedule["card_version"],
    }
    form_value = _values()
    service = CardActionService(
        ObservationRepository(database),
        daily_reviews=_service(database),
        observation_refresh=SimpleNamespace(
            on_observation_committed=lambda **_values: None
        ),
    )
    handled = []

    def handle_action(event):
        handled.append(event)
        return service.handle(
            person.id,
            message_id=event.message_id,
            callback_event_id=event.event_id,
            action_value=event.action_value,
            form_value=event.form_value,
        )

    server = FeishuCardCallbackServer(
        app_id="app",
        verification_token="verification-token",
        encrypt_key="encrypt-key",
        action_handler=handle_action,
        host="127.0.0.1",
        port=8123,
        path="/feishu/card/callback",
    )
    callback_body = json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_id": "daily-review-p2-event",
                "event_type": "card.action.trigger",
                "token": "verification-token",
                "app_id": "app",
                "tenant_key": "tenant",
            },
            "event": {
                "operator": {"open_id": "ou-daily-review-user"},
                "token": "update-token",
                "action": {
                    "tag": "button",
                    "name": "daily_review_submit",
                    "value": action_value,
                    "form_value": form_value,
                },
                "context": {
                    "open_message_id": "om-daily-review-card",
                    "open_chat_id": "oc-daily-review-chat",
                },
            },
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(now.timestamp()))
    nonce = "daily-review-nonce"
    signature = hashlib.sha256(
        (timestamp + nonce + "encrypt-key").encode() + callback_body
    ).hexdigest()
    async def post_callback():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://callback.test",
        ) as client:
            return await client.post(
                "/feishu/card/callback",
                content=callback_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": nonce,
                    "X-Lark-Signature": signature,
                },
            )

    callback_response = asyncio.run(post_callback())

    assert callback_response.status_code == 200
    assert callback_response.json()["toast"] == {
        "type": "success",
        "content": "每日回顾已记录并生成回顾估计。",
    }
    assert len(handled) == 1
    assert handled[0].action_value == action_value
    assert handled[0].form_value == form_value
    response = DailyReviewResponseRepository(database).latest(person.id, target)
    assert response is not None
    assert {
        "start_stress": response["start_stress"],
        "start_energy": response["start_energy"],
        "peak_stress": response["peak_stress"],
        "peak_period": response["peak_period"],
        "end_stress": response["end_stress"],
        "end_energy": response["end_energy"],
    } == {
        "start_stress": 3.0,
        "start_energy": 8.0,
        "peak_stress": 9.0,
        "peak_period": "evening",
        "end_stress": 5.0,
        "end_energy": 4.0,
    }
    assert response["energy_consumption"] == 7.0
    assert response["main_stressor"] == "presentation"
    assert response["recovery_note"] == "walk"
    assert response["free_text"] == "stable"
    retrospective = RetrospectiveCurveRepository(database).latest(person.id, target)
    assert retrospective is not None
    assert retrospective["daily_review_response_id"] == response["id"]
    assert retrospective["source_forecast_id"] == original["id"]
    unchanged = ForecastSnapshotRepository(database).latest(person.id, target)
    assert unchanged["id"] == original["id"]
    assert unchanged["forecast_version"] == original["forecast_version"]
    assert unchanged["curve"] == original["curve"]


def test_daily_review_energy_consumption_is_optional_diagnostic():
    database = memory_database()
    person = participant(database, "DR-OPTIONAL-ENERGY")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    values = _values()
    values.pop("energy_consumption")

    result = _service(database).submit(
        person.id,
        callback_event_id="callback-optional-energy",
        action=_daily_review_action(database, person.id, target, scheduled_at),
        values=values,
        submitted_at=scheduled_at + timedelta(minutes=30),
    )

    assert result["response"]["energy_consumption"] is None
    assert (
        result["retrospective"]["diagnostics"]["energy_consumption_diagnostic"]
        is None
    )
    assert (
        result["retrospective"]["diagnostics"][
            "energy_consumption_used_as_hard_anchor"
        ]
        is False
    )


def test_inconsistent_reported_peak_is_accepted_but_not_used_as_peak_anchor():
    database = memory_database()
    person = participant(database, "DR-INCONSISTENT-PEAK")
    target = date(2030, 1, 15)
    _seed_forecast(database, person.id, target)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)

    result = _service(database).submit(
        person.id,
        callback_event_id="callback-inconsistent-peak",
        action=_daily_review_action(database, person.id, target, scheduled_at),
        values=_values(start_stress="7", peak_stress="4", end_stress="8"),
        submitted_at=scheduled_at + timedelta(minutes=30),
    )

    assert result["response"]["peak_consistency"] is False
    diagnostics = result["retrospective"]["diagnostics"]
    assert diagnostics["peak_consistency"] is False
    assert diagnostics["peak_anchor_used"] is False
    assert diagnostics["peak_anchor_reason"] == (
        "inconsistent_reported_peak_not_used"
    )
    assert "peak_stress" not in {
        anchor["name"] for anchor in diagnostics["anchors"]
    }


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

        def get(self, _participant_id):
            return person

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
        forecasts=ForecastSnapshotRepository(database),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        catch_up_minutes=120,
        validity_minutes=1440,
    )
    restart = datetime(2030, 1, 15, 16, 5, tzinfo=timezone.utc)
    counts = asyncio.run(scheduler.run_once(restart))
    form = next(
        element
        for element in sent[0][1]["body"]["elements"]
        if element["tag"] == "form"
    )
    action = form["elements"][-1]["behaviors"][0]["value"]

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
        "ensured": 1, "sent": 1, "unavailable": 0,
        "source_forecast_unavailable": 0, "failed": 0,
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
        source_terminal_state={"stress_0_10": 4, "vitality_0_10": 5},
        end_anchor_minute=22 * 60,
        end_anchor_source="scheduled_review_time",
        review_local_date="2030-01-15",
        submitted_local_date="2030-01-16",
    )
    _, midnight_analysis, _ = service.reconstructor.reconstruct(
        _curve(), response,
        source_terminal_state={"stress_0_10": 4, "vitality_0_10": 5},
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

        def get(self, _participant_id):
            return person

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
        forecasts=ForecastSnapshotRepository(database),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
        catch_up_minutes=120,
        validity_minutes=1440,
    )
    recovered_at = datetime(2030, 1, 16, 2, 0, tzinfo=timezone.utc)
    assert asyncio.run(scheduler.run_once(recovered_at))["sent"] == 1
    recovered_card = sent[0][1]
    recovered_copy = str(recovered_card)
    assert "回顾 2030-01-15" in recovered_copy
    assert "如果这是次日补填" in recovered_copy
    assert "不要填写此刻状态" in recovered_copy
    assert "现在/今天结束时" not in recovered_copy
    form = next(
        element
        for element in recovered_card["body"]["elements"]
        if element["tag"] == "form"
    )
    action = form["elements"][-1]["behaviors"][0]["value"]
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
    assert schedules.authorize_claim_current(
        schedule["id"], claim["claim_token"], now=scheduled_at
    )
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


def test_direct_callback_without_forecast_persists_explicit_unresolved_source():
    database = memory_database()
    person = participant(database, "DR-NO-SOURCE-FORECAST")
    target = date(2030, 1, 15)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)

    result = _service(database).submit(
        person.id,
        callback_event_id="callback-no-source-forecast",
        action=_daily_review_action(
            database, person.id, target, scheduled_at
        ),
        values=_values(),
        submitted_at=scheduled_at + timedelta(minutes=5),
    )

    response = DailyReviewResponseRepository(database).latest(person.id, target)
    assert result["created"] is True
    assert result["retrospective"] is None
    assert response["causal_source_forecast_id"] is None
    assert response["causal_source_forecast_version"] is None
    assert RetrospectiveCurveRepository(database).latest(
        person.id, target
    ) is None
    assert _retrospective_count(database, person.id) == 0


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


def test_reactivated_forecast_is_persisted_as_daily_review_causal_source():
    database = memory_database()
    person = participant(database, "DR-REACTIVATED-SOURCE")
    target = date(2030, 1, 15)
    t0 = datetime(2030, 1, 15, 13, 50, tzinfo=timezone.utc)
    t1 = datetime(2030, 1, 15, 14, 10, tzinfo=timezone.utc)
    submitted_at = datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    forecast_v1 = _seed_forecast(
        database, person.id, target, version="forecast-reactivated-v1"
    )
    _set_forecast_timeline(database, forecast_v1["id"], t0)
    forecast_v2 = _seed_forecast(
        database, person.id, target, version="forecast-reactivated-v2"
    )
    _set_forecast_timeline(database, forecast_v2["id"], t1)
    reactivated_v1 = _seed_forecast(
        database, person.id, target, version="forecast-reactivated-v1"
    )
    _set_latest_currentness_event(
        database, forecast_v1["id"], t1 + timedelta(minutes=1)
    )

    forecasts = ForecastSnapshotRepository(database)
    assert reactivated_v1["id"] == forecast_v1["id"]
    assert _utc_timestamp(reactivated_v1["generated_at"]) == t0
    assert forecasts.latest(person.id, target)["id"] == forecast_v1["id"]

    result = _service(database).submit(
        person.id,
        callback_event_id="callback-reactivated-source",
        action=_daily_review_action(
            database,
            person.id,
            target,
            datetime(2030, 1, 15, 14, tzinfo=timezone.utc),
        ),
        values=_values(),
        submitted_at=submitted_at,
    )

    assert result["response"]["causal_source_forecast_id"] == forecast_v1["id"]
    assert (
        result["response"]["causal_source_forecast_version"]
        == "forecast-reactivated-v1"
    )
    assert result["retrospective"]["source_forecast_id"] == forecast_v1["id"]
    assert (
        result["retrospective"]["source_forecast_version"]
        == "forecast-reactivated-v1"
    )


def test_submission_excludes_forecast_generated_after_submitted_at():
    database = memory_database()
    person = participant(database, "DR-FUTURE-SOURCE-RACE")
    target = date(2030, 1, 15)
    before = datetime(2030, 1, 15, 14, 10, tzinfo=timezone.utc)
    submitted_at = datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    after = datetime(2030, 1, 15, 14, 31, tzinfo=timezone.utc)
    forecast_v1 = _seed_forecast(
        database, person.id, target, version="forecast-before-submission"
    )
    _set_forecast_timeline(database, forecast_v1["id"], before)
    forecast_v2 = _seed_forecast(
        database, person.id, target, version="forecast-after-submission"
    )
    _set_forecast_timeline(database, forecast_v2["id"], after)

    result = _service(database).submit(
        person.id,
        callback_event_id="callback-future-source-race",
        action=_daily_review_action(
            database,
            person.id,
            target,
            datetime(2030, 1, 15, 14, tzinfo=timezone.utc),
        ),
        values=_values(),
        submitted_at=submitted_at,
    )

    assert result["response"]["causal_source_forecast_id"] == forecast_v1["id"]
    assert result["retrospective"]["source_forecast_id"] == forecast_v1["id"]
    assert result["retrospective"]["source_forecast_id"] != forecast_v2["id"]


def test_reanalysis_uses_latest_facts_without_replacing_causal_artifact():
    database = memory_database()
    person = participant(database, "DR-LATEST-FACTS-REANALYSIS")
    target = date(2030, 1, 15)
    forecast_v1 = _seed_forecast(
        database, person.id, target, version="forecast-causal"
    )
    submitted_at = datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    service = _service(database)
    submitted = service.submit(
        person.id,
        callback_event_id="callback-reanalysis",
        action=_daily_review_action(
            database,
            person.id,
            target,
            datetime(2030, 1, 15, 14, tzinfo=timezone.utc),
        ),
        values=_values(),
        submitted_at=submitted_at,
    )
    causal = submitted["retrospective"]
    forecast_v2 = _seed_forecast(
        database, person.id, target, version="forecast-latest-facts"
    )

    latest_facts = service.reanalysis(person.id, target)
    persisted = RetrospectiveCurveRepository(database).latest(person.id, target)

    assert latest_facts["id"] is None
    assert latest_facts["analysis_kind"] == "reanalysis"
    assert latest_facts["diagnostics"]["analysis_kind"] == "reanalysis"
    assert latest_facts["source_forecast_id"] == forecast_v2["id"]
    assert persisted["id"] == causal["id"]
    assert persisted["source_forecast_id"] == forecast_v1["id"]
    assert _retrospective_count(database, person.id) == 1


def test_reactivated_forecast_crash_recovery_uses_persisted_exact_source():
    database = memory_database()
    person = participant(database, "DR-REACTIVATED-RECOVERY")
    target = date(2030, 1, 15)
    forecast_v1 = _seed_forecast(
        database, person.id, target, version="forecast-recovery-v1"
    )
    _seed_forecast(database, person.id, target, version="forecast-recovery-v2")
    reactivated_v1 = _seed_forecast(
        database, person.id, target, version="forecast-recovery-v1"
    )
    assert reactivated_v1["id"] == forecast_v1["id"]

    submitted_at = datetime(2030, 1, 15, 14, 30, tzinfo=timezone.utc)
    action = _daily_review_action(
        database,
        person.id,
        target,
        datetime(2030, 1, 15, 14, tzinfo=timezone.utc),
    )
    service = _service(database)
    rebuild = service.rebuild

    def crash_after_response(*_args, **_kwargs):
        raise RuntimeError("simulated reactivation recovery crash")

    service.rebuild = crash_after_response
    with pytest.raises(RuntimeError, match="reactivation recovery crash"):
        service.submit(
            person.id,
            callback_event_id="callback-reactivated-recovery",
            action=action,
            values=_values(),
            submitted_at=submitted_at,
        )
    response = DailyReviewResponseRepository(database).latest(person.id, target)
    assert response["causal_source_forecast_id"] == forecast_v1["id"]

    forecast_v3 = _seed_forecast(
        database, person.id, target, version="forecast-recovery-v3"
    )
    service.rebuild = rebuild
    recovered = service.submit(
        person.id,
        callback_event_id="callback-reactivated-recovery",
        action=action,
        values={},
        submitted_at=submitted_at + timedelta(minutes=10),
    )

    assert ForecastSnapshotRepository(database).latest(
        person.id, target
    )["id"] == forecast_v3["id"]
    assert recovered["created"] is False
    assert recovered["retrospective"]["source_forecast_id"] == forecast_v1["id"]
    assert (
        recovered["retrospective"]["source_forecast_id"]
        == recovered["response"]["causal_source_forecast_id"]
    )


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


def test_admin_rebuild_remains_causal_within_same_revision():
    database = memory_database()
    person = participant(database, "DR-SAME-REVISION-REBUILD")
    target = date(2030, 1, 15)
    forecast_v1 = _seed_forecast(
        database, person.id, target, version="forecast-admin-v1"
    )
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
    persisted_response = DailyReviewResponseRepository(database).latest(
        person.id, target
    )
    assert revision_two["retrospective"]["daily_review_revision"] == 2
    assert revision_two["response"]["causal_source_forecast_id"] == forecast_v1["id"]
    assert (
        persisted_response["causal_source_forecast_version"]
        == "forecast-admin-v1"
    )
    assert rebuilt["daily_review_revision"] == 2
    assert rebuilt["id"] == revision_two["retrospective"]["id"]
    assert rebuilt["source_forecast_version"] == "forecast-admin-v1"
    assert latest["id"] == revision_two["retrospective"]["id"]


def test_admin_curve_uses_exact_retrospective_source_for_stale_overlay():
    database = memory_database()
    person = participant(database, "DR-ADMIN-STALE-OVERLAY")
    target = date(2030, 1, 15)
    forecast_v1 = _seed_forecast(
        database, person.id, target, version="forecast-overlay-v1"
    )
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    result = _service(database).submit(
        person.id,
        callback_event_id="callback-admin-stale-overlay",
        action=_daily_review_action(
            database, person.id, target, scheduled_at
        ),
        values=_values(),
        submitted_at=scheduled_at + timedelta(minutes=20),
    )
    curve_v2 = [
        {
            **point,
            "stress_0_10": min(10.0, point["stress_0_10"] + 1.0),
        }
        for point in _curve()
    ]
    forecast_v2 = _seed_forecast(
        database,
        person.id,
        target,
        version="forecast-overlay-v2",
        curve=curve_v2,
    )

    class PressureCurves:
        async def read_persisted(self, participant_id, local_date, **_kwargs):
            forecast = ForecastSnapshotRepository(database).latest(
                participant_id, local_date
            )
            return PressureCurveView(
                forecast=forecast,
                analysis=analyze_curve(forecast["curve"]),
                png_bytes=b"",
            )

    browser = TestClient(
        create_app(database, _settings(), pressure_curves=PressureCurves())
    )
    login = browser.post(
        "/admin/api/login",
        json={"username": "root-admin", "password": "correct-password"},
    )
    assert login.status_code == 200
    response = browser.get(
        f"/admin/api/participants/{person.participant_code}"
        f"/pressure-curve/{target.isoformat()}"
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["forecast_version"] == forecast_v2["forecast_version"]
    assert payload["current_forecast_version"] == forecast_v2["forecast_version"]
    assert (
        payload["retrospective"]["source_forecast_version"]
        == forecast_v1["forecast_version"]
    )
    assert (
        payload["retrospective_source_forecast_id"]
        == result["retrospective"]["source_forecast_id"]
    )
    assert (
        payload["retrospective_source_forecast_version"]
        == forecast_v1["forecast_version"]
    )
    assert payload["retrospective_matches_current_forecast"] is False
    assert payload["retrospective_source_curve"] == forecast_v1["curve"]
    assert payload["retrospective_source_curve"] != payload["curve"]


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


def test_scheduler_cancels_pending_review_for_inactive_participant():
    database = memory_database()
    person = participant(database, "DR-INACTIVE-PENDING")
    target = date(2030, 1, 15)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedules = DailyReviewScheduleRepository(database)
    schedule = schedules.ensure(person.id, target, scheduled_at)
    _seed_forecast(database, person.id, target)
    binding_calls = []
    sent = []

    class Bindings:
        def get_for_participant(self, participant_id):
            binding_calls.append(participant_id)
            return {"chat_id": "inactive-chat"}

    class Sender:
        def send_card(self, *args, **kwargs):
            sent.append((args, kwargs))
            return "should-not-send"

    with database.session() as session:
        row = session.get(Participant, person.id)
        assert row is not None
        row.status = "inactive"
    participants = ParticipantRepository(database)
    assert person.id not in participants.active_ids()
    scheduler = DailyReviewScheduler(
        schedules=schedules,
        participants=participants,
        bindings=Bindings(),
        forecasts=ForecastSnapshotRepository(database),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
    )

    first = asyncio.run(scheduler.run_once(scheduled_at))
    second = asyncio.run(
        scheduler.run_once(scheduled_at + timedelta(minutes=1))
    )
    stored = schedules.get(schedule["id"])

    assert first["sent"] == 0
    assert second["sent"] == 0
    assert sent == []
    assert binding_calls == []
    assert stored["status"] == "cancelled"
    assert stored["last_error_code"] == "participant_inactive"
    assert stored["next_attempt_at"] is None
    assert stored["claim_token"] is None


def test_scheduler_rechecks_participant_status_after_claim_before_send():
    database = memory_database()
    person = participant(database, "DR-INACTIVE-AFTER-CLAIM")
    target = date(2030, 1, 15)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedules = DailyReviewScheduleRepository(database)
    _seed_forecast(database, person.id, target)
    repository = ParticipantRepository(database)
    binding_calls = []
    sent = []

    class Participants:
        def active_ids(self):
            return [person.id]

        def get(self, participant_id):
            with database.session() as session:
                row = session.get(Participant, participant_id)
                assert row is not None
                row.status = "inactive"
            return repository.get(participant_id)

    class Bindings:
        def get_for_participant(self, participant_id):
            binding_calls.append(participant_id)
            return {"chat_id": "race-chat"}

    class Sender:
        def send_card(self, *args, **kwargs):
            sent.append((args, kwargs))
            return "should-not-send"

    scheduler = DailyReviewScheduler(
        schedules=schedules,
        participants=Participants(),
        bindings=Bindings(),
        forecasts=ForecastSnapshotRepository(database),
        sender=Sender(),
        timezone_name="Asia/Shanghai",
        local_time="22:00",
    )

    counts = asyncio.run(scheduler.run_once(scheduled_at))
    schedule = schedules.ensure(person.id, target, scheduled_at)
    stored = schedules.get(schedule["id"])

    assert counts["ensured"] == 1
    assert counts["sent"] == 0
    assert sent == []
    assert len(binding_calls) == 1  # availability pass before the claim
    assert stored["status"] == "cancelled"
    assert stored["last_error_code"] == "participant_inactive"


def test_scheduler_defers_daily_review_when_source_forecast_is_missing():
    database = memory_database()
    person = participant(database, "DR-SCHEDULER-NO-FORECAST")
    target = date(2030, 1, 15)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    sent = []
    scheduler = _daily_review_scheduler(database, person.id, sent)

    counts = asyncio.run(scheduler.run_once(scheduled_at))
    schedule = DailyReviewScheduleRepository(database).ensure(
        person.id, target, scheduled_at
    )
    stored = DailyReviewScheduleRepository(database).get(schedule["id"])

    assert counts["sent"] == 0
    assert counts["source_forecast_unavailable"] == 1
    assert sent == []
    assert stored["status"] == "pending"
    assert stored["last_error_code"] == "source_forecast_unavailable"
    assert stored["attempt_count"] == 0
    assert stored["sent_at"] is None
    schedules = DailyReviewScheduleRepository(database)
    assert schedules.claim_due(
        scheduled_at + timedelta(days=1), 120
    ) == []
    assert schedules.get(schedule["id"])["status"] == "expired"


def test_scheduler_sends_once_after_source_forecast_recovers():
    database = memory_database()
    person = participant(database, "DR-SCHEDULER-FORECAST-RECOVERY")
    target = date(2030, 1, 15)
    scheduled_at = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    sent = []
    scheduler = _daily_review_scheduler(database, person.id, sent)

    missing = asyncio.run(scheduler.run_once(scheduled_at))
    _seed_forecast(
        database, person.id, target, version="forecast-delivery-recovered"
    )
    recovered = asyncio.run(
        scheduler.run_once(scheduled_at + timedelta(minutes=6))
    )
    repeated = asyncio.run(
        scheduler.run_once(scheduled_at + timedelta(minutes=7))
    )
    schedule = DailyReviewScheduleRepository(database).ensure(
        person.id, target, scheduled_at
    )

    assert missing["source_forecast_unavailable"] == 1
    assert recovered["sent"] == 1
    assert repeated["sent"] == 0
    assert len(sent) == 1
    stored_schedule = DailyReviewScheduleRepository(database).get(schedule["id"])
    assert stored_schedule["status"] == "sent"
    sent_chat_id, sent_card, message_uuid = sent[0]
    assert sent_chat_id == "chat-1"
    assert message_uuid == schedule["id"]
    assert sent_card["schema"] == "2.0"
    assert "elements" not in sent_card
    form = next(
        element
        for element in sent_card["body"]["elements"]
        if element["tag"] == "form"
    )
    button = next(
        element
        for element in form["elements"]
        if element.get("name") == "daily_review_submit"
    )
    assert button["behaviors"][0]["value"] == {
        "mindflow_action": "daily_review_submit",
        "version": "1",
        "schedule_id": schedule["id"],
        "local_date": target.isoformat(),
        "card_version": stored_schedule["card_version"],
    }


def test_schedule_claim_lease_retry_and_stable_message_uuid():
    database = memory_database()
    person = participant(database, "DR002")
    repo = DailyReviewScheduleRepository(database)
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    _seed_forecast(database, person.id, due.date())
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
        def get(self, _participant_id): return person
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
        forecasts=ForecastSnapshotRepository(database),
        timezone_name="Asia/Shanghai", local_time="22:00", poll_interval_seconds=60,
        retry_base_seconds=1, max_attempts=5, claim_lease_seconds=120,
    )
    counts = asyncio.run(scheduler.run_once(due + timedelta(seconds=123)))
    assert counts["sent"] == 1
    assert sent[0][2] == schedule["id"]
    assert asyncio.run(scheduler.run_once(due + timedelta(seconds=124)))["sent"] == 0


def test_schedule_retry_success_clears_last_error_class():
    database = memory_database()
    person = participant(database, "DR-ERROR-CLEAR-SENT")
    repo = DailyReviewScheduleRepository(database)
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedule = repo.ensure(person.id, due.date(), due)

    first_claim = repo.claim_due(due, 120)[0]
    assert repo.mark_failed(
        schedule["id"], first_claim["claim_token"], now=due,
        error=TimeoutError("temporary timeout"), max_attempts=3,
        retry_base_seconds=1,
    ) is True
    failed = repo.get(schedule["id"])
    assert failed["last_error_code"] == "delivery_failed"
    assert failed["last_error_class"] == "TimeoutError"

    retry_claim = repo.claim_due(due + timedelta(seconds=1), 120)[0]
    assert repo.authorize_claim_current(
        schedule["id"], retry_claim["claim_token"], now=due + timedelta(seconds=1)
    )
    assert repo.mark_sent(
        schedule["id"], retry_claim["claim_token"],
        now=due + timedelta(seconds=1), provider_message_id="message-after-retry",
    ) is True
    sent = repo.get(schedule["id"])
    assert sent["status"] == "sent"
    assert sent["last_error_code"] is None
    assert sent["last_error_class"] is None


def test_schedule_cancel_after_failure_clears_last_error_class():
    database = memory_database()
    person = participant(database, "DR-ERROR-CLEAR-CANCELLED")
    repo = DailyReviewScheduleRepository(database)
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedule = repo.ensure(person.id, due.date(), due)

    first_claim = repo.claim_due(due, 120)[0]
    assert repo.mark_failed(
        schedule["id"], first_claim["claim_token"], now=due,
        error=RuntimeError("temporary failure"), max_attempts=3,
        retry_base_seconds=1,
    ) is True
    assert repo.get(schedule["id"])["last_error_class"] == "RuntimeError"

    retry_claim = repo.claim_due(due + timedelta(seconds=1), 120)[0]
    assert repo.mark_cancelled(
        schedule["id"], retry_claim["claim_token"],
        now=due + timedelta(seconds=1), error_code="participant_inactive",
    ) is True
    cancelled = repo.get(schedule["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["last_error_code"] == "participant_inactive"
    assert cancelled["last_error_class"] is None


def test_schedule_failure_expiry_clears_last_error_class():
    database = memory_database()
    person = participant(database, "DR-ERROR-CLEAR-EXPIRED")
    repo = DailyReviewScheduleRepository(database)
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    valid_until = due + timedelta(seconds=2)
    schedule = repo.ensure(
        person.id, due.date(), due, valid_until=valid_until,
    )

    claim = repo.claim_due(due, 120)[0]
    assert repo.mark_failed(
        schedule["id"], claim["claim_token"], now=due,
        error=TimeoutError("temporary timeout"), max_attempts=3,
        retry_base_seconds=1,
    ) is True
    assert repo.get(schedule["id"])["last_error_class"] == "TimeoutError"

    assert repo.claim_due(valid_until, 120) == []
    expired = repo.get(schedule["id"])
    assert expired["status"] == "expired"
    assert expired["last_error_code"] == "delivery_window_expired"
    assert expired["last_error_class"] is None


def test_schedule_mark_failed_preserves_exception_class():
    database = memory_database()
    person = participant(database, "DR-ERROR-PRESERVE-FAILED")
    repo = DailyReviewScheduleRepository(database)
    due = datetime(2030, 1, 15, 14, tzinfo=timezone.utc)
    schedule = repo.ensure(person.id, due.date(), due)

    claim = repo.claim_due(due, 120)[0]
    assert repo.mark_failed(
        schedule["id"], claim["claim_token"], now=due,
        error=ConnectionError("delivery failed"), max_attempts=1,
        retry_base_seconds=1,
    ) is True
    failed = repo.get(schedule["id"])
    assert failed["status"] == "failed"
    assert failed["last_error_code"] == "delivery_failed"
    assert failed["last_error_class"] == "ConnectionError"


def test_old_unavailable_cards_expire_instead_of_batch_sending():
    database = memory_database()
    person = participant(database, "DR-EXPIRY")
    repo = DailyReviewScheduleRepository(database)
    _seed_forecast(database, person.id, date(2030, 1, 15))
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

        def get(self, _participant_id):
            return person

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
        forecasts=ForecastSnapshotRepository(database),
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
    _seed_forecast(database, person.id, date(2030, 1, 15))
    sent = []

    class Participants:
        def active_ids(self):
            return [person.id]

        def get(self, _participant_id):
            return person

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
        forecasts=ForecastSnapshotRepository(database),
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
    _seed_forecast(database, person.id, due.date())
    schedule = repo.ensure(person.id, date(2030, 1, 15), due)
    claimed = repo.claim_due(due, 120)[0]
    repo.mark_unavailable(
        schedule["id"], claimed["claim_token"], now=due
    )
    sent = []

    class Participants:
        def active_ids(self):
            return [person.id]

        def get(self, _participant_id):
            return person

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
        forecasts=ForecastSnapshotRepository(database),
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

        def get(self, _participant_id):
            return person

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
        forecasts=ForecastSnapshotRepository(database),
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
        "source_forecast_unavailable": 0,
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
