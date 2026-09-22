import asyncio
from datetime import date, datetime, timedelta, timezone
import uuid

import pytest

from app.models import WarningSchedule
from app.repositories import ForecastSnapshotRepository, ParticipantRepository
from app.services.forecast_scheduler import ForecastScheduler
from helpers import memory_database, participant, warning_repository
from tests.test_care_agent_composer import draft, packet


DAY = date(2030, 1, 15)
NOW = datetime(2030, 1, 15, 2, 0, tzinfo=timezone.utc)
FALLBACK = (
    "模型预计这段时间的压力可能上升，当前安排的负荷和恢复空间是主要依据。"
    "你可以先留出十分钟离开任务，走动或安静休息一下，再决定下一件最小可做的事；"
    "如果现在感觉还好，也可以忽略这条提醒。"
)


def _warning_setup(*, consent=True):
    database = memory_database()
    person = participant(database, f"CARE-DELIVERY-{uuid.uuid4().hex[:8]}")
    forecast = ForecastSnapshotRepository(database).save(
        person.id,
        DAY,
        calendar_revision="calendar-v1",
        semantic_revision="semantic-v1",
        observation_revision="observation-v1",
        algorithm_version="algorithm-v1",
        forecast_version="delivery-v1",
        semantic_status="rules_only",
        semantic_input=[],
        curve=[],
        peaks=[],
        warning_windows=[],
        output={},
    )
    warnings = warning_repository(database)
    warnings.sync(
        person.id,
        DAY,
        forecast_id=uuid.UUID(forecast["id"]),
        forecast_version=forecast["forecast_version"],
        warnings=[
            {
                "episode_identity": "delivery-episode",
                "target_time": NOW + timedelta(minutes=1),
                "valid_until": NOW + timedelta(hours=1),
                "risk_time": NOW + timedelta(hours=1),
                "warning_level": "2",
                "payload": {
                    "message": FALLBACK,
                    "fallback_message": FALLBACK,
                    "care_evidence": packet().to_dict(),
                    "care_plan": {
                        "intervention_type": "micro_break",
                        "action_minutes": 10,
                        "actions": ["ack"],
                    },
                },
            }
        ],
        now=NOW,
    )

    class Coordinator:
        async def _has_external_llm_consent(self, _participant_id):
            return consent

    class Bindings:
        def get_for_participant(self, _participant_id):
            return {"chat_id": "oc-care-delivery"}

    class Sender:
        def __init__(self, *, fail_first=False):
            self.calls = []
            self.fail_first = fail_first

        def send_text(self, chat_id, text, *, message_uuid=None):
            self.calls.append((chat_id, text, message_uuid))
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("transient sender failure")
            return f"om-{len(self.calls)}"

    return database, person, warnings, Coordinator(), Bindings(), Sender


def _scheduler(coordinator, warnings, bindings, sender, composer=None):
    return ForecastScheduler(
        coordinator=coordinator,
        participants=ParticipantRepository(warnings.database),
        warnings=warnings,
        bindings=bindings,
        sender=sender,
        timezone_name="Asia/Shanghai",
        daily_prepare_local_time="07:30",
        calendar_sync_interval_seconds=999,
        warning_poll_interval_seconds=999,
        calendar_oauth_app_id="calendar-app",
        care_composer=composer,
    )


class SuccessfulComposer:
    timeout_seconds = 1

    def __init__(self):
        self.calls = 0

    async def compose(self, _evidence):
        self.calls += 1
        return draft()


class InvalidComposer:
    timeout_seconds = 1

    async def compose(self, _evidence):
        return draft(intervention_type="schedule_adjustment")


class TimeoutComposer:
    timeout_seconds = 1

    async def compose(self, _evidence):
        await asyncio.sleep(1.1)


def _due(warnings, now=NOW + timedelta(minutes=2)):
    items = warnings.pending(now)
    assert len(items) == 1
    return items[0]


def test_no_external_llm_consent_skips_composer_and_sends_fallback():
    database, _, warnings, coordinator, bindings, Sender = _warning_setup(consent=False)
    composer = SuccessfulComposer()
    sender = Sender()

    asyncio.run(_scheduler(coordinator, warnings, bindings, sender, composer)._deliver_warning(_due(warnings)))

    assert composer.calls == 0
    assert sender.calls[0][1] == FALLBACK
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        assert row.payload_json.get("care_composition") is None


@pytest.mark.parametrize("composer", [InvalidComposer(), TimeoutComposer()])
def test_composer_failure_keeps_delivery_on_evidence_aware_fallback(composer):
    _, _, warnings, coordinator, bindings, Sender = _warning_setup(consent=True)
    sender = Sender()

    asyncio.run(_scheduler(coordinator, warnings, bindings, sender, composer)._deliver_warning(_due(warnings)))

    assert sender.calls[0][1] == FALLBACK


def test_validated_composition_is_persisted_and_reused_after_send_retry():
    database, _, warnings, coordinator, bindings, Sender = _warning_setup(consent=True)
    composer = SuccessfulComposer()
    sender = Sender(fail_first=True)
    scheduler = _scheduler(coordinator, warnings, bindings, sender, composer)

    asyncio.run(scheduler._deliver_warning(_due(warnings)))
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        assert row.status == "pending"
        assert row.payload_json["care_composition"]["mode"] == "agent"
        generated_message = row.payload_json["message"]

    retry_due = warnings.pending(NOW + timedelta(minutes=2))
    assert len(retry_due) == 1
    asyncio.run(scheduler._deliver_warning(retry_due[0]))

    assert composer.calls == 1
    assert sender.calls[-1][1] == generated_message
    with database.session() as session:
        assert session.query(WarningSchedule).one().status == "sent"
