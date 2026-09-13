import asyncio
from datetime import datetime, time, timedelta, timezone
import threading
import uuid

from app.agent.context import AgentContext, AuthorizationSemanticTurn
from app.models import ParticipantCarePreference, Reminder
from app.repositories import ParticipantRepository
from app.repositories_reminder import ReminderRepository
from app.services.proactive_notification_policy import ProactiveNotificationPolicy
from app.services.reminder_scheduler import ReminderScheduler
from app.tools.reminder import ReminderTools, has_exact_time_grounding
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


class _FailingSender:
    def __init__(self):
        self.thread_ids = []

    def send_text(self, *_args, **_kwargs):
        self.thread_ids.append(threading.get_ident())
        raise RuntimeError("provider unavailable")


class _ReminderStore:
    def __init__(self):
        self.created = []

    def create(self, participant_id, *, message, remind_at, recurrence_type):
        self.created.append((participant_id, message, remind_at, recurrence_type))
        return {
            "id": str(uuid.uuid4()), "participant_id": str(participant_id),
            "message": message, "remind_at": remind_at.isoformat(),
            "recurrence_type": recurrence_type,
        }


class _QuietPolicy:
    def user_requested_quiet_hours_warning(self, _participant_id, _remind_at):
        return False


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


def test_provider_failure_uses_worker_thread_and_durable_exponential_backoff():
    database = memory_database()
    user = participant(database, "REMINDER-RETRY")
    now = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)
    with database.session() as session:
        session.add(Reminder(
            participant_id=user.id, message="交作业", remind_at_utc=now,
            next_fire_at=now, recurrence_type="none", status="active",
        ))
    repo = ReminderRepository(database, timezone_name="Asia/Shanghai")
    sender = _FailingSender()
    scheduler = ReminderScheduler(
        reminders=repo, participants=ParticipantRepository(database),
        bindings=_Bindings(),
        proactive_policy=ProactiveNotificationPolicy(
            database, timezone_name="Asia/Shanghai", default_system_budget=0
        ),
        sender=sender, retry_base_seconds=60, max_attempts=2,
    )
    event_loop_thread = threading.get_ident()

    assert asyncio.run(scheduler.run_once(now))["failed"] == 1
    pending = repo.list_active(user.id)[0]
    assert pending["attempt_count"] == 1
    assert datetime.fromisoformat(pending["next_attempt_at"]) == now + timedelta(seconds=60)
    assert sender.thread_ids[0] != event_loop_thread
    assert asyncio.run(scheduler.run_once(now + timedelta(seconds=30)))["failed"] == 0
    assert len(sender.thread_ids) == 1

    assert asyncio.run(scheduler.run_once(now + timedelta(seconds=60)))["failed"] == 1
    with database.session() as session:
        row = session.query(Reminder).filter_by(participant_id=user.id).one()
        assert row.status == "delivery_failed"
        assert row.attempt_count == 2
        assert row.next_attempt_at is None


def test_reminder_time_must_be_grounded_in_user_words():
    base = dict(
        participant_id=uuid.uuid4(), participant_code="P", open_id="open",
        chat_id="chat", message_id="message", agent_run_id=uuid.uuid4(),
    )
    vague = AgentContext(
        **base, user_request_text="周三下午提醒我交作业"
    )
    exact = AgentContext(
        **base, user_request_text="周三15:00提醒我交作业"
    )
    clarified = AgentContext(
        **base,
        user_request_text="15:00",
        authorization_semantic_context=(
            AuthorizationSemanticTurn("user", "周三下午提醒我交作业"),
        ),
    )

    assert has_exact_time_grounding(vague) is False
    assert has_exact_time_grounding(exact) is True
    assert has_exact_time_grounding(clarified) is True


def test_natural_chinese_exact_reminder_times_are_grounded():
    base = dict(
        participant_id=uuid.uuid4(), participant_code="P", open_id="open",
        chat_id="chat", message_id="message", agent_run_id=uuid.uuid4(),
    )
    allowed = (
        "明天下午三点提醒我交作业",
        "周三三点半提醒我开会",
        "一小时后提醒我喝水",
        "半小时后提醒我休息",
        "两天后提醒我复习",
    )
    rejected = (
        "周三下午提醒我交作业",
        "明天提醒我交作业",
        "晚上提醒我",
    )

    for text in allowed:
        assert has_exact_time_grounding(
            AgentContext(**base, user_request_text=text)
        ) is True
    for text in rejected:
        assert has_exact_time_grounding(
            AgentContext(**base, user_request_text=text)
        ) is False


def test_reminder_grounding_does_not_compose_unrelated_historical_time():
    base = dict(
        participant_id=uuid.uuid4(), participant_code="P", open_id="open",
        chat_id="chat", message_id="message", agent_run_id=uuid.uuid4(),
    )
    vague_current = AgentContext(
        **base,
        user_request_text="周三下午提醒我交作业",
        authorization_semantic_context=(
            AuthorizationSemanticTurn("user", "明天15:00我有课"),
            AuthorizationSemanticTurn("user", "周三下午提醒我交作业"),
        ),
    )
    valid_clarification = AgentContext(
        **base,
        user_request_text="15:00",
        authorization_semantic_context=(
            AuthorizationSemanticTurn("user", "周三下午提醒我交作业"),
            AuthorizationSemanticTurn("assistant", "具体几点提醒你？"),
            AuthorizationSemanticTurn("user", "15:00"),
        ),
    )

    assert has_exact_time_grounding(vague_current) is False
    assert has_exact_time_grounding(valid_clarification) is True


def test_reminder_tool_persists_only_backend_grounded_absolute_time():
    received_at = datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)
    base = dict(
        participant_id=uuid.uuid4(), participant_code="P", open_id="open",
        chat_id="chat", message_id="message", agent_run_id=uuid.uuid4(),
        user_request_text="明天15:00提醒我交作业", received_at_utc=received_at,
    )
    store = _ReminderStore()
    tools = ReminderTools(
        store, _QuietPolicy(), timezone_name="Asia/Shanghai"
    )

    accepted = tools.create(AgentContext(**base), {
        "message": "交作业", "remind_at": "2026-09-14T15:00:00+08:00",
        "recurrence_type": "none",
    })
    wrong_day = tools.create(AgentContext(**base), {
        "message": "交作业", "remind_at": "2026-09-15T15:00:00+08:00",
        "recurrence_type": "none",
    })
    wrong_clock = tools.create(AgentContext(**base), {
        "message": "交作业", "remind_at": "2026-09-14T16:00:00+08:00",
        "recurrence_type": "none",
    })

    assert accepted["ok"] is True
    assert wrong_day["error"] == "reminder_time_not_grounded"
    assert wrong_clock["error"] == "reminder_time_not_grounded"
    assert len(store.created) == 1
    assert store.created[0][2].isoformat() == "2026-09-14T15:00:00+08:00"


def test_reminder_tool_persists_only_backend_grounded_relative_time():
    received_at = datetime(2026, 9, 13, 2, 15, tzinfo=timezone.utc)
    ctx = AgentContext(
        participant_id=uuid.uuid4(), participant_code="P", open_id="open",
        chat_id="chat", message_id="message", agent_run_id=uuid.uuid4(),
        user_request_text="一小时后提醒我喝水", received_at_utc=received_at,
    )
    store = _ReminderStore()
    tools = ReminderTools(store, _QuietPolicy(), timezone_name="Asia/Shanghai")

    accepted = tools.create(ctx, {
        "message": "喝水", "remind_at": "2026-09-13T03:15:00Z",
        "recurrence_type": "none",
    })
    rejected = tools.create(ctx, {
        "message": "喝水", "remind_at": "2026-09-13T04:15:00Z",
        "recurrence_type": "none",
    })

    assert accepted["ok"] is True
    assert rejected["error"] == "reminder_time_not_grounded"
    assert len(store.created) == 1
    assert store.created[0][2] == received_at + timedelta(hours=1)
