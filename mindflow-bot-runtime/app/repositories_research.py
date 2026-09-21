"""Participant-bound audit and short-lived evidence persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import uuid
from typing import Any

from sqlalchemy import delete

from app.contracts.research import ResearchEvidenceItem
from app.db import Database
from app.models import ResearchEvidence, ResearchJobAudit, utc_now


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ResearchRepository:
    def __init__(self, database: Database, *, evidence_ttl_hours: int = 6) -> None:
        self.database = database
        self.evidence_ttl = timedelta(hours=max(1, min(int(evidence_ttl_hours), 24)))

    def start_job(self, participant_id: uuid.UUID, *, topic: str, query: str, source_kind: str) -> str:
        now = utc_now()
        row = ResearchJobAudit(
            participant_id=participant_id,
            topic_hash=hashlib.sha256(topic.casefold().encode("utf-8")).hexdigest(),
            query_hash=hashlib.sha256(query.casefold().encode("utf-8")).hexdigest(),
            source_kind=source_kind,
            started_at=now,
        )
        with self.database.session() as session:
            session.add(row)
            session.flush()
            return str(row.id)

    def finish_job(self, job_id: str, *, status: str, failure_reason: str | None = None, request_count: int = 0, page_count: int = 0, browser_count: int = 0, exec_count: int = 0) -> None:
        with self.database.session() as session:
            row = session.get(ResearchJobAudit, uuid.UUID(str(job_id)), with_for_update=True)
            if row is None:
                return
            row.status = str(status)[:24]
            row.failure_reason = str(failure_reason)[:128] if failure_reason else None
            row.request_count = max(0, int(request_count))
            row.page_count = max(0, int(page_count))
            row.browser_count = max(0, int(browser_count))
            row.exec_count = max(0, int(exec_count))
            row.finished_at = utc_now()

    def save_evidence(self, participant_id: uuid.UUID, item: ResearchEvidenceItem, *, topic_label: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with self.database.session() as session:
            row = session.get(ResearchEvidence, item.evidence_id, with_for_update=True)
            if row is None:
                row = ResearchEvidence(
                    evidence_id=item.evidence_id,
                    participant_id=participant_id,
                    topic_label=topic_label or item.topic_label,
                    source_kind=item.source_kind,
                    title=item.title,
                    canonical_url=item.canonical_url,
                    publisher=item.publisher,
                    published_at=item.published_at,
                    updated_at=item.updated_at,
                    retrieved_at=_aware(datetime.fromisoformat(item.retrieved_at.replace("Z", "+00:00"))),
                    content=item.content,
                    content_hash=item.content_hash,
                    extraction_mode=item.extraction_mode,
                    freshness_hours=item.freshness_hours,
                    verified_public_source=item.verified_public_source,
                    expires_at=now + self.evidence_ttl,
                )
                session.add(row)
            elif row.participant_id != participant_id:
                raise ValueError("research evidence is participant-bound")
            return self._view(row)

    def purge_expired(self, *, now: datetime | None = None) -> int:
        instant = _aware(now or utc_now())
        with self.database.session() as session:
            result = session.execute(delete(ResearchEvidence).where(ResearchEvidence.expires_at <= instant))
            return int(result.rowcount or 0)

    @staticmethod
    def _view(row: ResearchEvidence) -> dict[str, Any]:
        return {
            "evidence_id": row.evidence_id, "participant_id": str(row.participant_id), "topic_label": row.topic_label,
            "source_kind": row.source_kind, "title": row.title, "canonical_url": row.canonical_url,
            "publisher": row.publisher, "published_at": row.published_at, "updated_at": row.updated_at, "retrieved_at": _aware(row.retrieved_at).isoformat(),
            "content": row.content, "content_hash": row.content_hash, "extraction_mode": row.extraction_mode,
            "freshness_hours": row.freshness_hours, "verified_public_source": row.verified_public_source,
            "expires_at": _aware(row.expires_at).isoformat(),
        }
