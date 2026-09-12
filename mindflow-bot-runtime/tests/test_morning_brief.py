import asyncio
from datetime import datetime, timezone

from app.repositories import ParticipantRepository
from app.repositories_morning_brief import MorningBriefScheduleRepository
from app.services.morning_brief_scheduler import (
    FORBIDDEN_BRIEF_TERMS,
    MorningBriefScheduler,
    render_morning_brief,
)
from app.services.proactive_notification_policy import ProactiveNotificationPolicy
from tests.helpers import memory_database, participant


class _Preferences:
    def get(self, _participant_id):
        return {"morning_brief_enabled": True, "morning_brief_local_time": "08:00"}


class _Bindings:
    def get_for_participant(self, _participant_id):
        return {"chat_id": "chat-1"}


class _Calendar:
    async def get_events(self, _participant_id, _start, _end):
        return [{"summary": "项目讨论", "start_time": "2026-09-12T09:00:00+08:00"}]


class _Sender:
    def __init__(self):
        self.messages = []

    def send_text(self, chat_id, text, *, message_uuid=None):
        self.messages.append((chat_id, text, message_uuid))
        return "provider-message"


def test_morning_brief_template_never_contains_prediction_fields():
    text = render_morning_brief(
        "2026-09-12",
        [{"summary": "上课", "start_time": "2026-09-12T08:30:00+08:00", "pressure": 9}],
        [{"message": "交作业", "risk": "high"}],
    )
    assert "上课" in text and "交作业" in text
    assert not any(term in text for term in FORBIDDEN_BRIEF_TERMS)
    assert "high" not in text


def test_opted_in_scheduler_sends_once_with_stable_provider_uuid():
    database = memory_database()
    user = participant(database, "MORNING-1")
    sender = _Sender()
    scheduler = MorningBriefScheduler(
        schedules=MorningBriefScheduleRepository(database),
        participants=ParticipantRepository(database),
        bindings=_Bindings(), care_preferences=_Preferences(),
        proactive_policy=ProactiveNotificationPolicy(
            database, timezone_name="Asia/Shanghai", default_system_budget=3
        ),
        calendar=_Calendar(), sender=sender,
    )
    now = datetime(2026, 9, 12, 0, 30, tzinfo=timezone.utc)
    first = asyncio.run(scheduler.run_once(now))
    second = asyncio.run(scheduler.run_once(now))

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert len(sender.messages) == 1
    assert sender.messages[0][2]
    assert "项目讨论" in sender.messages[0][1]
    assert str(user.id) not in sender.messages[0][1]


def test_default_off_creates_no_schedule():
    database = memory_database()
    participant(database, "MORNING-2")
    preferences = _Preferences()
    preferences.get = lambda _participant_id: {
        "morning_brief_enabled": False,
        "morning_brief_local_time": "08:00",
    }
    sender = _Sender()
    scheduler = MorningBriefScheduler(
        schedules=MorningBriefScheduleRepository(database),
        participants=ParticipantRepository(database), bindings=_Bindings(),
        care_preferences=preferences,
        proactive_policy=ProactiveNotificationPolicy(database, timezone_name="Asia/Shanghai", default_system_budget=3),
        calendar=_Calendar(), sender=sender,
    )
    result = asyncio.run(scheduler.run_once(datetime(2026, 9, 12, 0, 30, tzinfo=timezone.utc)))
    assert result == {"ensured": 0, "sent": 0, "failed": 0, "suppressed": 0}
    assert sender.messages == []
