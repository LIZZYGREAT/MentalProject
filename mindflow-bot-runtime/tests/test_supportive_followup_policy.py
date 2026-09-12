import asyncio
from datetime import datetime, time, timedelta, timezone

from app.models import ParticipantCarePreference
from app.repositories import ParticipantRepository
from app.repositories_followup import CareFollowupCandidateRepository
from app.services.proactive_notification_policy import ProactiveNotificationPolicy
from app.services.supportive_followup_scheduler import SupportiveFollowupScheduler
from tests.helpers import memory_database, participant


class _Bindings:
    def get_for_participant(self, _participant_id):
        return {"chat_id": "followup-chat"}


class _Sender:
    def __init__(self): self.sent = []
    def send_text(self, chat_id, text, *, message_uuid=None):
        self.sent.append((chat_id, text, message_uuid))


def test_followup_candidate_stores_only_neutral_category_and_ttl_is_bounded():
    database = memory_database()
    user = participant(database, "FOLLOWUP-1")
    repo = CareFollowupCandidateRepository(database)
    now = datetime.now(timezone.utc)
    repo.create(user.id, reason_category="check_in", due_at=now, ttl_hours=36)
    try:
        repo.create(user.id, reason_category="用户说我很焦虑", due_at=now)
    except ValueError:
        pass
    else:
        raise AssertionError("raw participant wording was accepted")
    try:
        repo.create(user.id, reason_category="check_in", due_at=now, ttl_hours=37)
    except ValueError:
        pass
    else:
        raise AssertionError("TTL beyond 36h was accepted")


def test_followup_is_suppressed_by_quiet_hours_and_allow_follow_up():
    database = memory_database()
    user = participant(database, "FOLLOWUP-2")
    now = datetime(2026, 9, 12, 0, 30, tzinfo=timezone.utc)
    with database.session() as session:
        session.add(ParticipantCarePreference(
            participant_id=user.id, allow_follow_up=True,
            quiet_hours_start=time(8, 0), quiet_hours_end=time(9, 0),
        ))
    candidates = CareFollowupCandidateRepository(database)
    candidates.create(user.id, reason_category="recovery", due_at=now)
    sender = _Sender()
    scheduler = SupportiveFollowupScheduler(
        candidates=candidates, participants=ParticipantRepository(database),
        bindings=_Bindings(), policy=ProactiveNotificationPolicy(
            database, timezone_name="Asia/Shanghai", default_system_budget=3
        ), sender=sender,
    )
    result = asyncio.run(scheduler.run_once(now))
    assert result["suppressed"] == 1
    assert sender.sent == []
