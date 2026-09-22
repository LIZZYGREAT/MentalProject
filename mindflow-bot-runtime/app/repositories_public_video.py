"""Participant-bound, short-lived public-video transcript cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select

from app.contracts.public_video import (
    VideoMetadata,
    VideoTranscriptDocument,
    transcript_as_dict,
    transcript_from_dict,
)
from app.db import Database
from app.models import PublicVideoCache, utc_now


class PublicVideoRepository:
    def __init__(self, database: Database, *, ttl_minutes: int = 60) -> None:
        self.database = database
        self.ttl_minutes = max(5, min(int(ttl_minutes), 360))

    def store_inspection(
        self,
        participant_id,
        *,
        metadata: VideoMetadata,
        transcript: VideoTranscriptDocument,
        max_chars: int,
        resource_key: str | None = None,
    ) -> dict:
        if transcript.total_chars > int(max_chars):
            raise ValueError("transcript_too_large")
        expires = utc_now() + timedelta(minutes=self.ttl_minutes)
        payload = json.dumps(
            transcript_as_dict(transcript), ensure_ascii=False, sort_keys=True
        )
        identity = str(
            resource_key
            or metadata.resource_key
            or f"{metadata.provider}:{metadata.video_id}"
        )[:192]
        with self.database.session() as session:
            existing = session.execute(
                select(PublicVideoCache)
                .where(
                    PublicVideoCache.participant_id == participant_id,
                    PublicVideoCache.provider == metadata.provider,
                    PublicVideoCache.video_id == metadata.video_id,
                    PublicVideoCache.resource_key == identity,
                )
                .order_by(PublicVideoCache.fetched_at.desc())
            ).scalars().first()
            row = existing or PublicVideoCache(
                participant_id=participant_id,
                provider=metadata.provider,
                video_id=metadata.video_id,
                resource_key=identity,
            )
            row.resource_key = identity
            row.canonical_url = metadata.canonical_url
            row.title = metadata.title[:300]
            row.description = metadata.description
            row.author = metadata.author
            row.duration_seconds = metadata.duration_seconds
            row.published_at = metadata.published_at
            row.cover_url = metadata.cover_url
            row.language = transcript.language
            row.subtitle_version = "v1"
            row.transcript_json = payload
            row.total_chars = transcript.total_chars
            row.extraction_mode = transcript.extraction_mode
            row.fetched_at = transcript_fetched_at = utc_now()
            row.expires_at = expires
            if existing is None:
                session.add(row)
            session.flush()
            return self._view(row, metadata, transcript)

    def get(
        self,
        participant_id,
        *,
        video_id: str,
        provider: str | None = None,
        resource_key: str | None = None,
    ) -> dict | None:
        with self.database.session() as session:
            query = select(PublicVideoCache).where(
                PublicVideoCache.participant_id == participant_id,
                PublicVideoCache.video_id == str(video_id),
                PublicVideoCache.expires_at > utc_now(),
            )
            if provider:
                query = query.where(PublicVideoCache.provider == str(provider))
            if resource_key:
                query = query.where(PublicVideoCache.resource_key == str(resource_key))
            row = session.execute(
                query.order_by(PublicVideoCache.fetched_at.desc())
            ).scalars().first()
            if row is None:
                return None
            try:
                transcript = transcript_from_dict(json.loads(row.transcript_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            metadata = VideoMetadata(
                provider=row.provider,
                canonical_url=row.canonical_url,
                title=row.title,
                description=row.description,
                author=row.author,
                duration_seconds=row.duration_seconds,
                published_at=row.published_at,
                cover_url=row.cover_url,
                video_id=row.video_id,
                resource_key=row.resource_key,
            )
            return self._view(row, metadata, transcript)

    def purge_expired(self, now: datetime | None = None) -> int:
        from sqlalchemy import delete

        instant = now or utc_now()
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        else:
            instant = instant.astimezone(timezone.utc)
        with self.database.session() as session:
            return session.execute(
                delete(PublicVideoCache).where(PublicVideoCache.expires_at <= instant)
            ).rowcount or 0

    @staticmethod
    def _view(
        row: PublicVideoCache,
        metadata: VideoMetadata,
        transcript: VideoTranscriptDocument,
    ) -> dict:
        return {
            "cache_id": str(row.id),
            "metadata": metadata,
            "transcript": transcript,
            "provider": row.provider,
            "video_id": row.video_id,
            "language": row.language,
            "subtitle_version": row.subtitle_version,
            "fetched_at": row.fetched_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
        }
