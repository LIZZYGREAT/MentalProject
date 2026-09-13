import asyncio
from datetime import datetime, time, timedelta, timezone
import threading
import uuid

from app.agent.context import AgentContext, AuthorizationSemanticTurn
from app.agent.tool_registry import ToolRegistry
from app.models import ParticipantCarePreference, Reminder
from app.repositories import ParticipantRepository
from app.repositories_reminder import ReminderRepository
from app.services.proactive_notification_policy import ProactiveNotificationPolicy
from app.services.reminder_scheduler import ReminderScheduler
from app.services.mutation_intent_verifier import (
    MutationIntentDecision,
    OpenAICompatibleMutationIntentClient,
)
from app.tools.reminder import ReminderTools
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


def _reminder_context(text: str, *, semantic_turns=()) -> AgentContext:
    return AgentContext(
        participant_id=uuid.uuid4(), participant_code="P", open_id="open",
        chat_id="chat", message_id="message", agent_run_id=uuid.uuid4(),
        user_request_text=text,
        received_at_utc=datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc),
        authorization_semantic_context=tuple(semantic_turns),
    )


def test_reminder_tool_accepts_timezone_aware_future_datetime():
    store = _ReminderStore()
    tools = ReminderTools(store, _QuietPolicy(), timezone_name="Asia/Shanghai")

    result = tools.create(_reminder_context("下周三下午三点提醒我交作业"), {
        "message": "交作业", "remind_at": "2099-09-23T15:00:00+08:00",
        "recurrence_type": "none",
    })

    assert result["ok"] is True
    assert len(store.created) == 1
    assert store.created[0][2].isoformat() == "2099-09-23T15:00:00+08:00"


def test_reminder_tool_rejects_invalid_naive_and_past_datetimes():
    store = _ReminderStore()
    tools = ReminderTools(store, _QuietPolicy(), timezone_name="Asia/Shanghai")
    ctx = _reminder_context("提醒我")

    invalid = tools.create(ctx, {
        "message": "测试", "remind_at": "not-a-datetime", "recurrence_type": "none",
    })
    naive = tools.create(ctx, {
        "message": "测试", "remind_at": "2099-09-23T15:00:00", "recurrence_type": "none",
    })
    past = tools.create(ctx, {
        "message": "测试", "remind_at": "2000-01-01T00:00:00Z", "recurrence_type": "none",
    })

    assert invalid["error"] == "invalid_reminder_datetime"
    assert naive["error"] == "reminder_timezone_required"
    assert past["error"] == "reminder_time_in_past"
    assert store.created == []


def test_reminder_authorization_context_contains_only_semantic_time_authority():
    tools = ReminderTools(_ReminderStore(), _QuietPolicy(), timezone_name="Asia/Shanghai")

    resolved = tools.authorization_context(_reminder_context("明天15:00提醒我"), {})

    assert resolved == {
        "reminder_time_context": {
            "reference_time_utc": "2026-09-13T02:00:00+00:00",
            "timezone": "Asia/Shanghai",
        },
        "reminder_semantic_contract": {
            "exact_time_required": True,
            "ambiguous_time_requires_clarification": True,
        },
    }
    serialized = str(resolved)
    assert "participant" not in serialized
    assert "open_id" not in serialized
    assert "chat_id" not in serialized


class _SemanticVerifier:
    def __init__(self, decision: str):
        self.decision = decision
        self.calls = []

    async def verify(self, **kwargs):
        self.calls.append(kwargs)
        if self.decision == "allow":
            return MutationIntentDecision("allow", "direct_action", "exact_reminder")
        if self.decision == "deny":
            return MutationIntentDecision("deny", "direct_action", "time_mismatch")
        return MutationIntentDecision("needs_clarification", "ambiguous", "time_ambiguous")


def test_reminder_registry_sends_proposal_reference_time_and_timezone_to_verifier():
    verifier = _SemanticVerifier("allow")
    registry = ToolRegistry(mutation_verifier=verifier)
    store = _ReminderStore()
    ReminderTools(store, _QuietPolicy(), timezone_name="Asia/Shanghai").register(registry)

    result = asyncio.run(registry.execute(
        _reminder_context("明天15:00提醒我交作业"),
        "reminder_create",
        {"message": "交作业", "remind_at": "2099-09-23T15:00:00+08:00", "recurrence_type": "none"},
    ))

    proposal = verifier.calls[0]["proposal_summary"]
    assert result.status == "succeeded"
    assert proposal["requested_values"]["remind_at"] == "2099-09-23T15:00:00+08:00"
    assert proposal["reminder_time_context"] == {
        "reference_time_utc": "2026-09-13T02:00:00+00:00",
        "timezone": "Asia/Shanghai",
    }
    assert len(store.created) == 1


def test_reminder_registry_blocks_ambiguous_or_mismatched_proposal_before_write():
    for decision, text, expected_status in (
        ("needs_clarification", "明天下午提醒我交作业", "mutation_needs_clarification"),
        ("deny", "明天15:00提醒我交作业", "tool_effect_not_authorized"),
    ):
        verifier = _SemanticVerifier(decision)
        registry = ToolRegistry(mutation_verifier=verifier)
        store = _ReminderStore()
        ReminderTools(store, _QuietPolicy(), timezone_name="Asia/Shanghai").register(registry)

        result = asyncio.run(registry.execute(
            _reminder_context(text),
            "reminder_create",
            {"message": "交作业", "remind_at": "2099-09-24T16:00:00+08:00", "recurrence_type": "none"},
        ))

        assert result.status == expected_status
        assert store.created == []


def test_reminder_clarification_turns_are_forwarded_to_semantic_verifier():
    turns = (
        AuthorizationSemanticTurn("user", "下周三下午提醒我交作业"),
        AuthorizationSemanticTurn("assistant", "具体几点？"),
        AuthorizationSemanticTurn("user", "三点"),
    )
    verifier = _SemanticVerifier("allow")
    registry = ToolRegistry(mutation_verifier=verifier)
    ReminderTools(_ReminderStore(), _QuietPolicy(), timezone_name="Asia/Shanghai").register(registry)

    asyncio.run(registry.execute(
        _reminder_context("三点", semantic_turns=turns),
        "reminder_create",
        {"message": "交作业", "remind_at": "2099-09-23T15:00:00+08:00", "recurrence_type": "none"},
    ))

    assert verifier.calls[0]["semantic_turn_context"] == (
        {"role": "user", "text": "下周三下午提醒我交作业"},
        {"role": "assistant", "text": "具体几点？"},
        {"role": "user", "text": "三点"},
    )


def test_mutation_verifier_prompt_keeps_reminder_semantics_out_of_backend_parser():
    prompt = OpenAICompatibleMutationIntentClient.SYSTEM_PROMPT

    assert "For reminder_create" in prompt
    assert "exact proposed remind_at" in prompt
    assert "backend-provided reference time and timezone" in prompt
    assert "return needs_clarification" in prompt
    assert "Do not borrow unrelated dates or times" in prompt
