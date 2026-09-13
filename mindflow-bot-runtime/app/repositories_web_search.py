"""Short-lived participant-bound cache for controlled search evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from typing import Any

from sqlalchemy import delete, select

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

    def record_failure(
        self,
        participant_id,
        *,
        query_hash: str,
        freshness: str,
        error_code: str,
        provider: str | None = None,
        ttl_minutes: int,
    ) -> None:
        now = utc_now()
        with self.database.session() as session:
            session.add(WebSearchRun(
                participant_id=participant_id, query_hash=query_hash,
                normalized_query=None, freshness=freshness,
                status="failed", error_code=str(error_code)[:64],
                provider=str(provider)[:32] if provider else None,
                expires_at=now + timedelta(minutes=ttl_minutes),
            ))

    def record_success(
        self,
        participant_id,
        *,
        query_hash: str,
        freshness: str,
        provider: str,
        provider_summary: str,
        provider_request_id: str | None,
        items: list[dict[str, Any]],
        ttl_minutes: int,
    ) -> list[dict[str, Any]]:
        now = utc_now()
        expires = now + timedelta(minutes=ttl_minutes)
        with self.database.session() as session:
            run = WebSearchRun(
                participant_id=participant_id, query_hash=query_hash,
                normalized_query=None, freshness=freshness,
                status="succeeded", provider=str(provider)[:32],
                provider_summary=str(provider_summary or "")[:8000] or None,
                provider_request_id=(
                    str(provider_request_id)[:128] if provider_request_id else None
                ),
                expires_at=expires,
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
                    snippet="",
                    content=None,
                    published_at=_published(
                        item.get("published_at") or item.get("page_age")
                    ),
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

    def get_run(self, participant_id, run_id: str) -> dict[str, Any] | None:
        try:
            parsed_id = uuid.UUID(str(run_id))
        except ValueError:
            return None
        with self.database.session() as session:
            run = session.execute(select(WebSearchRun).where(
                WebSearchRun.id == parsed_id,
                WebSearchRun.participant_id == participant_id,
                WebSearchRun.expires_at > utc_now(),
            )).scalar_one_or_none()
            if run is None:
                return None
            items = session.execute(select(WebSearchResult).where(
                WebSearchResult.run_id == run.id,
                WebSearchResult.participant_id == participant_id,
                WebSearchResult.expires_at > utc_now(),
            ).order_by(WebSearchResult.rank)).scalars().all()
            return {
                "id": str(run.id),
                "query_hash": run.query_hash,
                "freshness": run.freshness,
                "status": run.status,
                "provider": run.provider,
                "provider_summary": run.provider_summary,
                "provider_request_id": run.provider_request_id,
                "results": [self._view(item) for item in items],
            }

    def purge_expired(self, now: datetime | None = None) -> dict[str, int]:
        """Physically remove expired evidence and its parent audit rows."""

        instant = now or utc_now()
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        else:
            instant = instant.astimezone(timezone.utc)
        with self.database.session() as session:
            # Delete children explicitly because SQLite does not always enable
            # foreign-key cascades in lightweight/test deployments.
            result_count = session.execute(
                delete(WebSearchResult).where(WebSearchResult.expires_at <= instant)
            ).rowcount or 0
            run_count = session.execute(
                delete(WebSearchRun).where(WebSearchRun.expires_at <= instant)
            ).rowcount or 0
        return {"results": result_count, "runs": run_count}

    @staticmethod
    def _view(row: WebSearchResult) -> dict[str, Any]:
        return {
            "id": str(row.id), "title": row.title, "source_url": row.source_url,
            "run_id": str(row.run_id),
            "snippet": row.snippet, "content": row.content,
            "published_at": row.published_at.isoformat() if row.published_at else None,
        }
