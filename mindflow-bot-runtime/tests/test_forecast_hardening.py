import asyncio
from datetime import date, datetime, timedelta, timezone
import io
import logging
import uuid

from algorithm.dynamic_state_model import assess_event
from app.agent.context import AgentContext
from app.integrations.feishu.client import FeishuSendError
from app.logging_security import install_credential_redaction
from app.models import FeishuOAuthToken, WarningSchedule
from app.repositories import (
    BindingRepository, ForecastSnapshotRepository, ParticipantRepository,
)
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.forecast_scheduler import ForecastScheduler
from app.tools.care import CareTools
from helpers import memory_database
from services.semantic_model_inputs import semantic_model_inputs
from tests.test_forecast_pipeline import build_pipeline, event
from utils.event_factory import EventFactory


def semantic(appraisal, difficulty=0.6):
    values = {
        "difficulty": difficulty, "cognitive_demand": 0.6, "stakes": 0.5,
        "time_pressure": 0.4, "social_evaluation": 0.4,
        "uncontrollability": 0.3, "novelty": 0.2,
        "expected_effort": 0.6, "uncertainty": 0.3, "unfinished": 0.2,
    }
    return {
        "values": values,
        "fused": {"objective_semantics": values, "appraisal_score_1_10": appraisal},
    }


def assessed(appraisal, explicit=None):
    metadata = {"semantic": semantic(appraisal)}
    if explicit is not None:
        metadata["appraisal"] = explicit
    model_event = EventFactory.create_from_json([{
        **event(), "event_type": "task", "task_type": "meeting",
        "metadata": metadata,
    }])[0]
    return assess_event(model_event)


def test_fused_appraisal_enters_model_and_explicit_appraisal_wins():
    negative = assessed(1)
    positive = assessed(10)
    assert negative.appraisal["threat"] > positive.appraisal["threat"]
    assert negative.appraisal["challenge"] < positive.appraisal["challenge"]
    explicit = assessed(1, {"threat": 0.05, "challenge": 0.95})
    assert explicit.appraisal["threat"] == 0.05
    assert explicit.appraisal["challenge"] == 0.95


def test_appraisal_is_part_of_model_projection_and_materiality():
    before = [{"id": "e", "metadata": {"semantic": semantic(5.0)}}]
    material = [{"id": "e", "metadata": {"semantic": semantic(9.0)}}]
    tiny = [{"id": "e", "metadata": {"semantic": semantic(5.01)}}]
    assert ForecastCoordinator._semantic_delta(before, material) >= 0.03
    assert ForecastCoordinator._semantic_delta(before, tiny) < 0.03
    assert semantic_model_inputs(semantic(9.0))["appraisal_f_like"] == 0.8


def test_semantic_tasks_are_cleaned_and_close_is_bounded():
    class Client:
        provider = "deepseek"
        model = "fake"

        def infer(self, _payload):
            return {
                "values": {key: 0.5 for key in (
                    "difficulty", "cognitive_demand", "stakes", "time_pressure",
                    "social_evaluation", "uncontrollability", "novelty",
                    "expected_effort", "uncertainty", "unfinished",
                )},
                "appraisal_score_1_10": 5, "confidence": 0.8,
                "evidence_tags": [], "reasoning_summary": "ok",
            }

    _, participant, _, preprocessor, _, _, _ = build_pipeline(
        [event()], consent=True, client=Client()
    )

    async def scenario():
        misses = []
        for index in range(100):
            _, _, _, item_misses = preprocessor.prepare(
                participant.id,
                [event(summary=f"课程{index}")], consent=True,
            )
            misses.extend(item_misses)
        await preprocessor.enqueue(participant.id, misses, lambda: asyncio.sleep(0))
        await asyncio.sleep(0.5)
        await preprocessor.close(timeout_seconds=2)
        assert preprocessor._inflight == {}
        assert not preprocessor._completion_tasks

    asyncio.run(scenario())


def test_active_calendar_ids_excludes_participant_without_oauth():
    database = memory_database()
    participants = ParticipantRepository(database)
    connected = participants.create("CONNECTED")
    participants.create("NO-OAUTH")
    now = datetime.now(timezone.utc)
    with database.session() as session:
        session.add(FeishuOAuthToken(
            participant_id=connected.id, access_token_ciphertext="x",
            refresh_token_ciphertext="y", access_token_expires_at=now + timedelta(hours=1),
        ))
    assert participants.active_calendar_ids() == [connected.id]


def test_scheduler_bounds_concurrency_and_logs_job_failure(caplog):
    class Participants:
        def active_calendar_ids(self):
            return [uuid.uuid4() for _ in range(4)]

    class Coordinator:
        def __init__(self):
            self.running = 0
            self.maximum = 0
            self.calls = 0

        async def ensure_forecast(self, _pid, _target, _reason):
            self.running += 1
            self.maximum = max(self.maximum, self.running)
            self.calls += 1
            try:
                await asyncio.sleep(0.01)
                if self.calls == 2:
                    raise RuntimeError("safe failure")
            finally:
                self.running -= 1

    coordinator = Coordinator()
    scheduler = ForecastScheduler(
        coordinator=coordinator, participants=Participants(), warnings=None,
        bindings=None, sender=None, timezone_name="Asia/Shanghai",
        daily_prepare_local_time="07:30", calendar_sync_interval_seconds=999,
        warning_poll_interval_seconds=999, forecast_max_concurrency=2,
    )

    async def scenario():
        task = asyncio.create_task(scheduler._forecast_loop())
        while coordinator.calls < 8:
            await asyncio.sleep(0.01)
        await scheduler.close()
        await task

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert coordinator.maximum <= 2
    assert "forecast_job_failed" in caplog.text


def test_warning_retry_lease_expiry_and_missing_channel():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, date.today(), "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        warning_id = row.id
        now = row.target_time + timedelta(seconds=1)
    claimed = warnings.claim_if_current(warning_id, now=now, lease_seconds=10)
    assert claimed is not None
    assert warnings.claim_if_current(warning_id, now=now + timedelta(seconds=5), lease_seconds=10) is None
    assert warnings.claim_if_current(warning_id, now=now + timedelta(seconds=11), lease_seconds=10) is not None
    warnings.finish_claim(
        warning_id, sent=False, now=now + timedelta(seconds=11), retryable=True,
        max_attempts=5, retry_base_seconds=60, error_class="FeishuSendError",
    )
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "pending"
        assert row.attempt_count == 1
        assert row.next_attempt_at >= now + timedelta(seconds=70)
        row.valid_until = now + timedelta(seconds=20)
    assert warnings.pending(now + timedelta(seconds=21)) == []
    with database.session() as session:
        assert session.get(WarningSchedule, warning_id).status == "expired"


def test_warning_episode_time_drift_dedupes_and_tier_escalates():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        return await coordinator.ensure_forecast(participant.id, date.today(), "daily_prepare")

    first = asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.status = "sent"
        row.sent_at = datetime.now(timezone.utc)
        original_id = row.id
        original_risk = row.risk_time
        episode = row.episode_identity
        forecast_id = row.forecast_id
        version = row.forecast_version
    now = datetime.now(timezone.utc)
    drifted = [{
        "warning_identity": episode, "episode_identity": episode,
        "target_time": original_risk - timedelta(minutes=15),
        "risk_time": original_risk + timedelta(minutes=5),
        "valid_until": original_risk + timedelta(minutes=20),
        "warning_level": "2", "episode_drift_minutes": 15, "payload": {},
    }]
    diff = warnings.sync(
        participant.id, date.today(), forecast_id=forecast_id,
        forecast_version=version, warnings=drifted, now=now,
    )
    assert diff["kept"] == 1
    with database.session() as session:
        assert session.query(WarningSchedule).count() == 1
        row = session.get(WarningSchedule, original_id)
        row.warning_level = "1"
        row.status = "sent"
    drifted[0]["warning_level"] = "3"
    warnings.sync(
        participant.id, date.today(), forecast_id=forecast_id,
        forecast_version=version, warnings=drifted, now=now,
    )
    with database.session() as session:
        row = session.get(WarningSchedule, original_id)
        assert row.status == "pending"
        assert row.payload_json["escalation"] is True


def test_same_trigger_far_outside_drift_window_creates_a_new_episode():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        return await coordinator.ensure_forecast(participant.id, date.today(), "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.status = "sent"
        episode = row.episode_identity
        risk_time = row.risk_time
        forecast_id = row.forecast_id
        version = row.forecast_version
    later_risk = risk_time + timedelta(hours=3)
    warnings.sync(
        participant.id, date.today(), forecast_id=forecast_id,
        forecast_version=version, now=datetime.now(timezone.utc), warnings=[{
            "warning_identity": episode, "episode_identity": episode,
            "target_time": later_risk - timedelta(minutes=20),
            "risk_time": later_risk,
            "valid_until": later_risk + timedelta(minutes=10),
            "warning_level": "2", "episode_drift_minutes": 15,
            "payload": {},
        }],
    )
    with database.session() as session:
        rows = session.query(WarningSchedule).all()
        assert len(rows) == 2
        assert {row.status for row in rows} == {"sent", "pending"}


def test_claimed_row_without_lease_is_recovered_after_migration_compatibility():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(participant.id, date.today(), "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        now = datetime.now(timezone.utc)
        row.status = "claimed"
        row.lease_until = None
        row.target_time = now - timedelta(seconds=1)
        row.next_attempt_at = now - timedelta(seconds=1)
        row.valid_until = now + timedelta(minutes=10)
        warning_id = row.id
    due = warnings.pending(datetime.now(timezone.utc))
    assert [item["id"] for item in due] == [str(warning_id)]


def test_today_context_returns_latest_forecast():
    database, participant, _, _, _, _, coordinator = build_pipeline([event()])

    async def prepare():
        return await coordinator.ensure_forecast(participant.id, date.today(), "user_curve_request")

    generated = asyncio.run(prepare())
    tools = CareTools(
        profiles=type("Profiles", (), {"current": lambda self, _pid: None})(),
        observations=type("Obs", (), {"recent": lambda self, _pid, limit=1: []})(),
        predictions=type("Pred", (), {"latest": lambda self, _pid: None})(),
        prediction_service=None, calendar=None, tokens=None,
        timezone_name="Asia/Shanghai", forecast_coordinator=coordinator,
        forecast_snapshots=ForecastSnapshotRepository(database),
    )
    context = tools.get_today_context(
        AgentContext(
            participant_id=participant.id, participant_code="P", message_id="m",
            open_id="o", chat_id="c", agent_run_id=uuid.uuid4(),
        ), {}
    )
    assert context["latest_forecast"]["forecast_version"] == generated["forecast_version"]


def test_nonretryable_feishu_failure_is_not_hot_retried():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    class Sender:
        def send_text(self, *_args):
            raise FeishuSendError("no", code=230001, retryable=False)

    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc_test"}

    async def scenario():
        await coordinator.ensure_forecast(participant.id, date.today(), "daily_prepare")
        with database.session() as session:
            row = session.query(WarningSchedule).one()
            now = datetime.now(timezone.utc)
            row.target_time = now - timedelta(seconds=1)
            row.next_attempt_at = now - timedelta(seconds=1)
            row.valid_until = now + timedelta(minutes=10)
        item = warnings.pending(datetime.now(timezone.utc))[0]
        scheduler = ForecastScheduler(
            coordinator=coordinator, participants=None, warnings=warnings,
            bindings=Bindings(), sender=Sender(), timezone_name="Asia/Shanghai",
            daily_prepare_local_time="07:30", calendar_sync_interval_seconds=999,
            warning_poll_interval_seconds=999,
        )
        await scheduler._deliver_warning(item)

    asyncio.run(scenario())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert row.next_attempt_at is None
        assert row.last_error_code == "230001"


def test_missing_chat_binding_is_rechecked_without_consuming_send_attempt():
    database, participant, _, _, _, warnings, coordinator = build_pipeline([event()])

    class Bindings:
        available = False

        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc_test"} if self.available else None

    async def scenario():
        await coordinator.ensure_forecast(participant.id, date.today(), "daily_prepare")
        with database.session() as session:
            row = session.query(WarningSchedule).one()
            now = datetime.now(timezone.utc)
            row.target_time = now - timedelta(seconds=1)
            row.next_attempt_at = now - timedelta(seconds=1)
            row.valid_until = now + timedelta(minutes=10)
        item = warnings.pending(datetime.now(timezone.utc))[0]
        bindings = Bindings()
        scheduler = ForecastScheduler(
            coordinator=coordinator, participants=None, warnings=warnings,
            bindings=bindings, sender=None, timezone_name="Asia/Shanghai",
            daily_prepare_local_time="07:30", calendar_sync_interval_seconds=999,
            warning_poll_interval_seconds=999,
        )
        await scheduler._deliver_warning(item)
        with database.session() as session:
            row = session.query(WarningSchedule).one()
            assert row.status == "delivery_unavailable"
            assert row.attempt_count == 0
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        bindings.available = True
        await scheduler._recover_delivery_channels()

    asyncio.run(scenario())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        assert row.status == "pending"
        assert row.attempt_count == 0


def test_calendar_refresh_failure_is_persisted_and_returned_as_stale():
    database, participant, calendar, _, _, _, coordinator = build_pipeline([event()])

    async def scenario():
        fresh = await coordinator.ensure_forecast(participant.id, date.today(), "daily_prepare")
        assert fresh["calendar_fresh"] is True

        async def fail(*_args):
            raise TimeoutError("calendar unavailable")

        calendar.get_events = fail
        stale = await coordinator.ensure_forecast(participant.id, date.today(), "periodic_poll")
        assert stale["calendar_stale"] is True
        assert stale["calendar_last_refresh_error_class"] == "TimeoutError"
        assert stale["calendar_last_refresh_success_at"] is not None

    asyncio.run(scenario())


def test_log_record_factory_redacts_feishu_query_credentials():
    install_credential_redaction()
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("redaction-test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info(
            "connect %s", "wss://example.test?access_key=secret-a&ticket=secret-b"
        )
    finally:
        logger.removeHandler(handler)
    rendered = output.getvalue()
    assert "secret-a" not in rendered
    assert "secret-b" not in rendered
    assert "access_key=[redacted]" in rendered
    assert "ticket=[redacted]" in rendered
