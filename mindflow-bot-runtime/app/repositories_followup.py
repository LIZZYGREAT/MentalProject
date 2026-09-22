"""TTL-bounded supportive follow-up candidates and delivery claims."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import or_, select

from app.db import Database
from app.models import CareFollowupCandidate, Participant, utc_now


REASON_CATEGORIES = frozenset({"check_in", "task_transition", "recovery"})


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class CareFollowupCandidateRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, participant_id: uuid.UUID, *, reason_category: str, due_at: datetime, ttl_hours: int = 24) -> str:
        if reason_category not in REASON_CATEGORIES:
            raise ValueError("unsupported neutral follow-up reason")
        if not 1 <= int(ttl_hours) <= 36:
            raise ValueError("follow-up TTL must be between 1 and 36 hours")
        due = _aware(due_at)
        with self.database.session() as session:
            if session.get(Participant, participant_id) is None:
                raise ValueError("participant does not exist")
            row = CareFollowupCandidate(
                participant_id=participant_id, reason_category=reason_category,
                due_at=due, expires_at=due + timedelta(hours=int(ttl_hours)),
            )
            session.add(row)
            session.flush()
            return str(row.id)

    def claim_due(self, now: datetime, lease_seconds: int = 120):
        instant = _aware(now)
        with self.database.session() as session:
            expired = session.execute(select(CareFollowupCandidate).where(
                CareFollowupCandidate.status == "pending",
                CareFollowupCandidate.expires_at <= instant,
            ).with_for_update()).scalars().all()
            for row in expired:
                row.status = "expired"
                row.updated_at = instant
            rows = session.execute(select(CareFollowupCandidate).where(
                CareFollowupCandidate.status == "pending",
                CareFollowupCandidate.due_at <= instant,
                CareFollowupCandidate.expires_at > instant,
                or_(CareFollowupCandidate.claim_token.is_(None), CareFollowupCandidate.lease_until <= instant),
            ).order_by(CareFollowupCandidate.due_at).limit(100).with_for_update()).scalars().all()
            result = []
            for row in rows:
                row.claim_token = uuid.uuid4()
                row.lease_until = instant + timedelta(seconds=max(1, lease_seconds))
                result.append({
                    "id": str(row.id), "participant_id": str(row.participant_id),
                    "reason_category": row.reason_category,
                    "due_at": _aware(row.due_at).isoformat(),
                    "claim_token": str(row.claim_token),
                })
            return result

    def finish(self, candidate_id: str, claim_token: str, *, status: str, now: datetime) -> bool:
        with self.database.session() as session:
            row = session.execute(select(CareFollowupCandidate).where(
                CareFollowupCandidate.id == uuid.UUID(candidate_id),
                CareFollowupCandidate.claim_token == uuid.UUID(claim_token),
            ).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            row.status = status
            row.sent_at = _aware(now) if status == "sent" else None
            row.claim_token = None
            row.lease_until = None
            row.updated_at = _aware(now)
            return True
