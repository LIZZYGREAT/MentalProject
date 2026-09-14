"""Participant-bound, short-lived cache for public web documents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, select

from app.db import Database
from app.models import WebDocument, WebDocumentChunk, utc_now


class WebDocumentRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def store(
        self,
        participant_id,
        *,
        url_hash: str,
        canonical_url: str,
        title: str,
        content_type: str,
        chunks: list[str],
        fetched_at: datetime,
        ttl_minutes: int,
    ) -> dict:
        expires = utc_now() + timedelta(minutes=ttl_minutes)
        with self.database.session() as session:
            document = WebDocument(
                participant_id=participant_id,
                url_hash=str(url_hash)[:64],
                canonical_url=str(canonical_url)[:4000],
                title=str(title or "Untitled")[:300],
                content_type=str(content_type)[:128],
                chunk_count=len(chunks),
                fetched_at=fetched_at,
                expires_at=expires,
            )
            session.add(document)
            session.flush()
            for index, content in enumerate(chunks):
                session.add(WebDocumentChunk(
                    document_id=document.id,
                    participant_id=participant_id,
                    chunk_index=index,
                    content=str(content),
                    expires_at=expires,
                ))
            session.flush()
            return self._view(document, chunks[0])

    def get_by_url_hash(self, participant_id, url_hash: str) -> dict | None:
        with self.database.session() as session:
            document = session.execute(
                select(WebDocument)
                .where(
                    WebDocument.participant_id == participant_id,
                    WebDocument.url_hash == str(url_hash),
                    WebDocument.expires_at > utc_now(),
                )
                .order_by(WebDocument.fetched_at.desc())
            ).scalars().first()
            if document is None:
                return None
            chunk = session.execute(select(WebDocumentChunk).where(
                WebDocumentChunk.document_id == document.id,
                WebDocumentChunk.participant_id == participant_id,
                WebDocumentChunk.chunk_index == 0,
                WebDocumentChunk.expires_at > utc_now(),
            )).scalar_one_or_none()
            return self._view(document, chunk.content) if chunk else None

    def get_chunk(self, participant_id, document_id: str, chunk_index: int) -> dict | None:
        result = self.get_chunks(
            participant_id,
            document_id,
            start_index=chunk_index,
            count=1,
        )
        if result is None:
            return None
        content = result["chunks"][0]["content"]
        return self._view(result["document"], content, chunk_index=int(chunk_index))

    def get_chunks(
        self,
        participant_id,
        document_id: str,
        *,
        start_index: int,
        count: int,
    ) -> dict | None:
        try:
            parsed_id = uuid.UUID(str(document_id))
        except ValueError:
            return None
        start = int(start_index)
        requested_count = int(count)
        if start < 0 or requested_count < 1:
            return None
        with self.database.session() as session:
            document = session.execute(
                select(WebDocument)
                .where(
                    WebDocument.id == parsed_id,
                    WebDocument.participant_id == participant_id,
                    WebDocument.expires_at > utc_now(),
                )
            ).scalar_one_or_none()
            if document is None or start >= document.chunk_count:
                return None
            rows = session.execute(
                select(WebDocumentChunk)
                .where(
                    WebDocumentChunk.document_id == document.id,
                    WebDocumentChunk.participant_id == participant_id,
                    WebDocumentChunk.chunk_index >= start,
                    WebDocumentChunk.chunk_index < min(
                        start + requested_count,
                        document.chunk_count,
                    ),
                    WebDocumentChunk.expires_at > utc_now(),
                )
                .order_by(WebDocumentChunk.chunk_index.asc())
            ).scalars().all()
            expected = list(range(start, start + len(rows)))
            if not rows or [row.chunk_index for row in rows] != expected:
                return None
            return {
                "document": document,
                "chunks": [
                    {"chunk_index": row.chunk_index, "content": row.content}
                    for row in rows
                ],
            }

    def purge_expired(self, now: datetime | None = None) -> dict[str, int]:
        instant = now or utc_now()
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        else:
            instant = instant.astimezone(timezone.utc)
        with self.database.session() as session:
            chunks = session.execute(
                delete(WebDocumentChunk).where(WebDocumentChunk.expires_at <= instant)
            ).rowcount or 0
            documents = session.execute(
                delete(WebDocument).where(WebDocument.expires_at <= instant)
            ).rowcount or 0
        return {"document_chunks": chunks, "documents": documents}

    @staticmethod
    def _view(
        document: WebDocument,
        content: str,
        *,
        chunk_index: int = 0,
    ) -> dict:
        return {
            "document_id": str(document.id),
            "title": document.title,
            "source_url": document.canonical_url,
            "content_type": document.content_type,
            "chunk_count": document.chunk_count,
            "chunk_index": chunk_index,
            "content": content,
            "fetched_at": document.fetched_at.isoformat(),
        }
