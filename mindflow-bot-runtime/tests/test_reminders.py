import asyncio
from datetime import datetime, time, timedelta, timezone
import json
from types import SimpleNamespace
import threading
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.card_actions.registry import card_action_spec
from app.models import ParticipantCarePreference, Reminder, ReminderProposal
from app.repositories import ObservationRepository, ParticipantRepository
from app.repositories_reminder import ReminderRepository
from app.services.card_action_service import CardActionService
from app.services.presentation_service import PresentationOutbox
from app.services.proactive_notification_policy import ProactiveNotificationPolicy
from app.services.reminder_scheduler import ReminderScheduler
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


def _reminder_context(participant_id: uuid.UUID, text: str) -> AgentContext:
    return AgentContext(
        participant_id=participant_id, participant_code="P", open_id="open",
        chat_id="chat", message_id="message", agent_run_id=uuid.uuid4(),
        user_request_text=text,
        received_at_utc=datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc),
    )


def _proposal_stack(database, participant_id, *, verifier=None):
    repository = ReminderRepository(database, timezone_name="Asia/Shanghai")
    outbox = PresentationOutbox()
    tools = ReminderTools(
        repository, _QuietPolicy(), outbox, timezone_name="Asia/Shanghai"
    )
    registry = ToolRegistry(mutation_verifier=verifier)
    tools.register(registry)
    card_actions = CardActionService(
        ObservationRepository(database),
        observation_refresh=SimpleNamespace(),
        reminders=repository,
        timezone_name="Asia/Shanghai",
    )
    return repository, outbox, registry, card_actions


def _proposal_action(card, action_name: str):
    for element in card["body"]["elements"]:
        for behavior in element.get("behaviors") or []:
            value = dict(behavior.get("value") or {})
            if value.get("mindflow_action") == action_name:
                return value
    raise AssertionError(f"missing {action_name}")


class _FailIfCalledVerifier:
    def __init__(self):
        self.calls = []

    async def verify(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("proposal staging must not invoke semantic verifier")


def test_reminder_agent_semantics_are_reviewed_before_persist():
    database = memory_database()
    owner = participant(database, "REMINDER-PROPOSAL")
    verifier = _FailIfCalledVerifier()
    repository, outbox, registry, _card_actions = _proposal_stack(
        database, owner.id, verifier=verifier
    )
    context = _reminder_context(owner.id, "下周三下午三点提醒我交作业")

    result = asyncio.run(
        registry.execute(
            context,
            "reminder_create",
            {
                "message": "交作业",
                "remind_at": "2099-09-23T15:00:00+08:00",
                "recurrence_type": "none",
            },
        )
    )

    assert result.result["reminder_proposal"] == "pending_confirmation"
    assert result.result["persisted"] is False
    assert repository.list_active(owner.id) == []
    assert verifier.calls == []
    spec = next(spec for spec in registry.specs if spec.name == "reminder_create")
    assert spec.effect == "proposal_stage"
    assert spec.authorization_requirement == "none"
    assert spec.authorization_context_resolver is None
    with database.session() as session:
        assert session.query(ReminderProposal).count() == 1


def test_reminder_confirmation_displays_resolved_local_time_then_persists():
    database = memory_database()
    owner = participant(database, "REMINDER-CONFIRM")
    repository, outbox, registry, card_actions = _proposal_stack(
        database, owner.id
    )
    context = _reminder_context(owner.id, "明天下午三点提醒我交作业")
    staged = asyncio.run(
        registry.execute(
            context,
            "reminder_create",
            {
                "message": "交作业",
                "remind_at": "2099-09-23T07:00:00+00:00",
                "recurrence_type": "none",
            },
        )
    )
    card = outbox.take_cards(context.agent_run_id)[0]
    serialized = json.dumps(card, ensure_ascii=False)
    assert "交作业" in serialized
    assert "2099-09-23 15:00" in serialized
    assert repository.list_active(owner.id) == []

    confirmed = card_actions.handle(
        owner.id,
        message_id="reminder-card",
        callback_event_id="reminder-confirm-event",
        action_value=_proposal_action(card, "reminder_proposal_confirm"),
        form_value={},
    )

    assert confirmed["ok"] is True
    assert confirmed["status"] == "confirmed"
    assert [item["message"] for item in repository.list_active(owner.id)] == [
        "交作业"
    ]


def test_reminder_cancel_before_confirm_persists_nothing():
    database = memory_database()
    owner = participant(database, "REMINDER-PROPOSAL-CANCEL")
    repository, outbox, registry, card_actions = _proposal_stack(
        database, owner.id
    )
    context = _reminder_context(owner.id, "明天下午三点提醒我交作业")
    asyncio.run(
        registry.execute(
            context,
            "reminder_create",
            {
                "message": "交作业",
                "remind_at": "2099-09-23T15:00:00+08:00",
                "recurrence_type": "none",
            },
        )
    )
    card = outbox.take_cards(context.agent_run_id)[0]

    cancelled = card_actions.handle(
        owner.id,
        message_id="reminder-card",
        callback_event_id="reminder-cancel-event",
        action_value=_proposal_action(card, "reminder_proposal_cancel"),
        form_value={},
    )

    assert cancelled["ok"] is True
    assert cancelled["persisted"] is False
    assert repository.list_active(owner.id) == []


def test_reminder_cancel_tool_stages_before_cancelling_active_reminder():
    database = memory_database()
    owner = participant(database, "REMINDER-CANCEL-REVIEW")
    repository, outbox, registry, card_actions = _proposal_stack(
        database, owner.id
    )
    active = repository.create(
        owner.id,
        message="喝水",
        remind_at=datetime.now(timezone.utc) + timedelta(days=10),
    )
    context = _reminder_context(owner.id, "取消喝水提醒")

    staged = asyncio.run(
        registry.execute(
            context,
            "reminder_cancel",
            {"reminder_id": active["id"]},
        )
    )
    assert staged.result["persisted"] is False
    assert len(repository.list_active(owner.id)) == 1
    card = outbox.take_cards(context.agent_run_id)[0]
    assert "喝水" in json.dumps(card, ensure_ascii=False)

    confirmed = card_actions.handle(
        owner.id,
        message_id="cancel-card",
        callback_event_id="cancel-confirm-event",
        action_value=_proposal_action(card, "reminder_proposal_confirm"),
        form_value={},
    )
    assert confirmed["operation"] == "cancel"
    assert repository.list_active(owner.id) == []


def test_reminder_proposal_is_participant_bound_and_confirm_action_needs_receipt():
    database = memory_database()
    owner = participant(database, "REMINDER-PROPOSAL-OWNER")
    other = participant(database, "REMINDER-PROPOSAL-OTHER")
    repository, outbox, registry, card_actions = _proposal_stack(
        database, owner.id
    )
    context = _reminder_context(owner.id, "提醒我")
    staged = asyncio.run(
        registry.execute(
            context,
            "reminder_create",
            {
                "message": "测试",
                "remind_at": "2099-09-23T15:00:00+08:00",
                "recurrence_type": "none",
            },
        )
    )
    card = outbox.take_cards(context.agent_run_id)[0]
    denied = card_actions.handle(
        other.id,
        message_id="copied-card",
        action_value=_proposal_action(card, "reminder_proposal_confirm"),
        form_value={},
    )

    assert denied["error"] == "reminder_proposal_not_found"
    assert repository.list_active(owner.id) == []
    with database.session() as session:
        proposal_id = session.query(ReminderProposal.id).scalar()
    assert repository.get_proposal_for_participant(
        owner.id, proposal_id
    )["status"] == "awaiting_confirmation"
    assert card_action_spec("reminder_proposal_confirm").replay_policy == "receipt_required"


def test_reminder_tool_rejects_invalid_naive_and_past_datetimes():
    database = memory_database()
    owner = participant(database, "REMINDER-INVALID")
    repository = ReminderRepository(database, timezone_name="Asia/Shanghai")
    tools = ReminderTools(
        repository,
        _QuietPolicy(),
        PresentationOutbox(),
        timezone_name="Asia/Shanghai",
    )
    ctx = _reminder_context(owner.id, "提醒我")

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
    assert repository.list_active(owner.id) == []
