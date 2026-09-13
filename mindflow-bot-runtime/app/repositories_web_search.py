"""Short-lived participant-bound cache for controlled search evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from typing import Any

from sqlalchemy import select

from app.db import Database
from app.models import WebSearchResult, WebSearchRun, utc_now


def _published(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class WebSearchRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record_failure(self, participant_id, *, query_hash: str, freshness: str, error_code: str, ttl_minutes: int) -> None:
        now = utc_now()
        with self.database.session() as session:
            session.add(WebSearchRun(
                participant_id=participant_id, query_hash=query_hash,
                normalized_query=None, freshness=freshness,
                status="failed", error_code=str(error_code)[:64],
                expires_at=now + timedelta(minutes=ttl_minutes),
            ))

    def record_success(self, participant_id, *, query_hash: str, freshness: str, items: list[dict[str, Any]], ttl_minutes: int) -> list[dict[str, Any]]:
        now = utc_now()
        expires = now + timedelta(minutes=ttl_minutes)
        with self.database.session() as session:
            run = WebSearchRun(
                participant_id=participant_id, query_hash=query_hash,
                normalized_query=None, freshness=freshness,
                status="succeeded", expires_at=expires,
            )
            session.add(run)
            session.flush()
            rows = []
            for rank, item in enumerate(items, start=1):
                source_url = str(item.get("url") or item.get("source_url") or "")[:4000]
                if not source_url.startswith(("https://", "http://")):
                    continue
                row = WebSearchResult(
                    run_id=run.id, participant_id=participant_id, rank=rank,
                    source_url=source_url,
                    title=str(item.get("title") or "Untitled")[:300],
                    snippet=str(item.get("snippet") or "")[:4000],
                    content=str(item.get("content") or "")[:20000] or None,
                    published_at=_published(item.get("published_at")),
                    retrieved_at=now, expires_at=expires,
                )
                session.add(row)
                session.flush()
                rows.append(self._view(row))
            return rows

    def get_result(self, participant_id, result_id: str) -> dict[str, Any] | None:
        try:
            parsed_id = uuid.UUID(str(result_id))
        except ValueError:
            return None
        with self.database.session() as session:
            row = session.execute(select(WebSearchResult).where(
                WebSearchResult.id == parsed_id,
                WebSearchResult.participant_id == participant_id,
                WebSearchResult.expires_at > utc_now(),
            )).scalar_one_or_none()
            return self._view(row) if row else None

    @staticmethod
    def _view(row: WebSearchResult) -> dict[str, Any]:
        return {
            "id": str(row.id), "title": row.title, "source_url": row.source_url,
            "snippet": row.snippet, "content": row.content,
            "published_at": row.published_at.isoformat() if row.published_at else None,
        }
