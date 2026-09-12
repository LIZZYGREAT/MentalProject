import asyncio
from datetime import datetime, time, timedelta, timezone

from app.models import ParticipantCarePreference, Reminder
from app.repositories import ParticipantRepository
from app.repositories_reminder import ReminderRepository
from app.services.proactive_notification_policy import ProactiveNotificationPolicy
from app.services.reminder_scheduler import ReminderScheduler
from tests.helpers import memory_database, participant


class _Bindings:
    def get_for_participant(self, _participant_id):
        return {"chat_id": "chat-reminders"}


class _Sender:
    def __init__(self):
        self.messages = []

    def send_text(self, chat_id, text, *, message_uuid=None):
        self.messages.append((chat_id, text, message_uuid))
        return "provider-id"


def test_reminder_repository_is_participant_bound_and_limited():
    database = memory_database()
    first = participant(database, "REMINDER-1")
    second = participant(database, "REMINDER-2")
    repo = ReminderRepository(database, timezone_name="Asia/Shanghai", active_limit=1)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    created = repo.create(first.id, message="  交   作业  ", remind_at=future)

    assert created["message"] == "交 作业"
    assert repo.list_active(second.id) == []
    try:
        repo.create(first.id, message="第二条", remind_at=future)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("active reminder limit was not enforced")
    assert repo.cancel(second.id, created["id"]) is False
    assert repo.cancel(first.id, created["id"]) is True


def test_user_requested_reminder_fires_during_mute_and_outside_system_budget():
    database = memory_database()
    user = participant(database, "REMINDER-3")
    now = datetime(2026, 9, 12, 0, 30, tzinfo=timezone.utc)
    with database.session() as session:
        session.add(ParticipantCarePreference(
            participant_id=user.id,
            quiet_hours_start=time(8, 0), quiet_hours_end=time(9, 0),
            global_proactive_muted_until=now + timedelta(days=1),
            max_system_proactive_per_day=0,
        ))
        session.add(Reminder(
            participant_id=user.id, message="喝水", remind_at_utc=now,
            next_fire_at=now, recurrence_type="none", status="active",
        ))
    repo = ReminderRepository(database, timezone_name="Asia/Shanghai")
    policy = ProactiveNotificationPolicy(database, timezone_name="Asia/Shanghai", default_system_budget=0)
    sender = _Sender()
    scheduler = ReminderScheduler(
        reminders=repo, participants=ParticipantRepository(database),
        bindings=_Bindings(), proactive_policy=policy, sender=sender,
    )

    result = asyncio.run(scheduler.run_once(now))

    assert result["sent"] == 1
    assert sender.messages[0][1] == "提醒：喝水"
    assert policy.user_requested_quiet_hours_warning(user.id, now) is True
    assert repo.list_active(user.id) == []


def test_weekly_recurrence_preserves_local_clock():
    database = memory_database()
    user = participant(database, "REMINDER-4")
    repo = ReminderRepository(database, timezone_name="Asia/Shanghai")
    now = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)
    with database.session() as session:
        session.add(Reminder(
            participant_id=user.id, message="周报", remind_at_utc=now,
            next_fire_at=now, recurrence_type="weekly", weekday=6,
            status="active", claim_token=None,
        ))
    claimed = repo.claim_due(now, 120)
    assert repo.mark_fired(claimed[0]["id"], claimed[0]["claim_token"], fired_at=now)
    remaining = repo.list_active(user.id)
    assert datetime.fromisoformat(remaining[0]["next_fire_at"]) == now + timedelta(days=7)
