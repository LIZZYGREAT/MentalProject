from datetime import datetime, time, timedelta, timezone

from app.models import ParticipantCarePreference
from app.services.proactive_notification_policy import ProactiveNotificationPolicy
from tests.helpers import memory_database, participant


def test_system_messages_share_budget_but_user_reminders_do_not():
    database = memory_database()
    user = participant(database, "PROACTIVE-1")
    policy = ProactiveNotificationPolicy(database, timezone_name="Asia/Shanghai", default_system_budget=1)
    due = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)

    first = policy.reserve(user.id, message_kind="morning_brief", dedupe_key="brief:2026-09-12", scheduled_at=due)
    second = policy.reserve(user.id, message_kind="daily_review", dedupe_key="review:2026-09-12", scheduled_at=due)
    reminder = policy.reserve(user.id, message_kind="reminder", dedupe_key="reminder:1", scheduled_at=due)

    assert first.allowed is True
    assert second == second.__class__(False, "daily_budget")
    assert reminder.allowed is True


def test_global_mute_and_quiet_hours_only_suppress_system_messages():
    database = memory_database()
    user = participant(database, "PROACTIVE-2")
    due = datetime(2026, 9, 12, 0, 30, tzinfo=timezone.utc)  # 08:30 local
    with database.session() as session:
        session.add(ParticipantCarePreference(
            participant_id=user.id,
            quiet_hours_start=time(8, 0), quiet_hours_end=time(9, 0),
            global_proactive_muted_until=due + timedelta(hours=1),
        ))
    policy = ProactiveNotificationPolicy(database, timezone_name="Asia/Shanghai", default_system_budget=3)

    system = policy.reserve(user.id, message_kind="daily_review", dedupe_key="system", scheduled_at=due)
    reminder = policy.reserve(user.id, message_kind="reminder", dedupe_key="user", scheduled_at=due)

    assert system.reason == "global_mute"
    assert reminder.allowed is True
    assert reminder.quiet_hours_warning is True


def test_reservation_is_deduplicated_and_can_be_released():
    database = memory_database()
    user = participant(database, "PROACTIVE-3")
    policy = ProactiveNotificationPolicy(database, timezone_name="Asia/Shanghai", default_system_budget=2)
    due = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)
    first = policy.reserve(user.id, message_kind="warning", dedupe_key="episode-1", scheduled_at=due)
    assert first.allowed and first.reservation_id
    assert policy.reserve(user.id, message_kind="warning", dedupe_key="episode-1", scheduled_at=due).reason == "duplicate"
    assert policy.release(first.reservation_id, reason="provider_failed") is True
