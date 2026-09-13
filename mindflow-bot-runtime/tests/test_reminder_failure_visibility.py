from datetime import datetime, timedelta, timezone
import uuid

from app.agent.context import AgentContext
from app.models import Reminder
from app.repositories_reminder import ReminderRepository
from app.services.proactive_notification_policy import ProactiveNotificationPolicy
from app.tools.reminder import ReminderTools
from tests.helpers import memory_database, participant


def test_recent_failed_reminder_is_visible_only_to_its_owner():
    database = memory_database()
    owner = participant(database, "REMINDER-FAILED-LIST")
    other = participant(database, "REMINDER-FAILED-OTHER")
    now = datetime.now(timezone.utc)
    repository = ReminderRepository(database, timezone_name="Asia/Shanghai")
    repository.create(
        owner.id, message="未来提醒", remind_at=now + timedelta(days=3)
    )
    with database.session() as session:
        session.add_all([
            _failed(owner.id, "未送达提醒", now - timedelta(days=1)),
            _failed(owner.id, "过期失败提醒", now - timedelta(days=8)),
            _failed(other.id, "他人的失败提醒", now - timedelta(days=1)),
        ])
    context = AgentContext(
        participant_id=owner.id,
        participant_code="P",
        open_id="open",
        chat_id="chat",
        message_id="message",
        agent_run_id=uuid.uuid4(),
    )
    policy = ProactiveNotificationPolicy(
        database, timezone_name="Asia/Shanghai", default_system_budget=3
    )

    result = ReminderTools(
        repository, policy, timezone_name="Asia/Shanghai"
    ).list(context, {})

    assert [item["message"] for item in result["reminders"]] == [
        "未来提醒", "未送达提醒"
    ]
    failed = result["reminders"][1]
    assert failed["status"] == "delivery_failed"
    assert failed["delivery_notice"] == "这条提醒未能成功送达"


def _failed(participant_id, message: str, updated_at: datetime) -> Reminder:
    return Reminder(
        participant_id=participant_id,
        message=message,
        remind_at_utc=updated_at - timedelta(hours=1),
        next_fire_at=None,
        recurrence_type="none",
        status="delivery_failed",
        attempt_count=5,
        last_error_code="provider_failed",
        updated_at=updated_at,
    )
