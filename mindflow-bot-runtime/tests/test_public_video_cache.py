import asyncio

from app.contracts.public_video import (
    TranscriptSegment,
    VideoMetadata,
    VideoTranscriptDocument,
)
from app.repositories_public_video import PublicVideoRepository
from app.services.public_video_service import PublicVideoService
from helpers import memory_database, participant


def metadata(video_id="BV-cache"):
    return VideoMetadata(
        provider="fake",
        canonical_url=f"https://video.example/{video_id}",
        title="Cached video",
        description="Description",
        author="Author",
        duration_seconds=30,
        published_at=None,
        cover_url=None,
        video_id=video_id,
    )


def transcript(video_id="BV-cache"):
    segments = tuple(
        TranscriptSegment(float(index), float(index + 1), f"segment-{index}")
        for index in range(4)
    )
    return VideoTranscriptDocument(
        video_id=video_id,
        provider="fake",
        title="Cached video",
        language="en",
        segments=segments,
        total_chars=len("\n".join(segment.text for segment in segments)),
        extraction_mode="public_subtitle",
    )


class CountingAdapter:
    provider = "fake"

    def __init__(self):
        self.transcript_calls = 0

    def can_handle(self, url):
        return url.startswith("https://video.example/")

    async def resolve_metadata(self, url):
        return metadata()

    async def resolve_transcript(self, metadata_value):
        self.transcript_calls += 1
        return transcript(metadata_value.video_id)


def test_public_video_repository_round_trips_metadata_and_transcript():
    database = memory_database()
    user = participant(database, "VIDEO-CACHE-1")
    repository = PublicVideoRepository(database, ttl_minutes=60)

    repository.store_inspection(
        user.id,
        metadata=metadata(),
        transcript=transcript(),
        max_chars=120_000,
    )
    cached = repository.get(user.id, video_id="BV-cache", provider="fake")

    assert cached is not None
    assert cached["metadata"].title == "Cached video"
    assert cached["transcript"].segments == transcript().segments
    assert cached["transcript"].language == "en"


def test_video_cache_is_participant_bound_and_transcript_is_reused():
    database = memory_database()
    first = participant(database, "VIDEO-CACHE-A")
    second = participant(database, "VIDEO-CACHE-B")
    repository = PublicVideoRepository(database)
    adapter = CountingAdapter()
    service = PublicVideoService(repository, adapters=(adapter,))

    first_result = asyncio.run(
        service.inspect_url(first.id, url="https://video.example/BV-cache")
    )
    second_service = PublicVideoService(repository, adapters=(adapter,))
    second_result = asyncio.run(
        second_service.inspect_url(first.id, url="https://video.example/BV-cache")
    )

    assert first_result["cache_hit"] is False
    assert second_result["cache_hit"] is True
    assert adapter.transcript_calls == 1
    assert repository.get(second.id, video_id="BV-cache", provider="fake") is None


def test_video_read_transcript_returns_bounded_chunks_and_offsets():
    database = memory_database()
    user = participant(database, "VIDEO-CHUNK")
    repository = PublicVideoRepository(database)
    service = PublicVideoService(repository, adapters=())
    repository.store_inspection(
        user.id,
        metadata=metadata(),
        transcript=transcript(),
        max_chars=120_000,
    )

    first = asyncio.run(
        service.read_transcript(
            user.id, video_id="BV-cache", offset=0, limit_chars=10
        )
    )
    second = asyncio.run(
        service.read_transcript(
            user.id,
            video_id="BV-cache",
            offset=first["next_offset"],
            limit_chars=10,
        )
    )

    assert first["ok"] is True
    assert len(first["text"]) <= 10
    assert first["done"] is False
    assert second["offset"] == first["next_offset"]
    assert second["text"]


def test_video_read_transcript_requires_a_known_participant_video():
    database = memory_database()
    user = participant(database, "VIDEO-UNKNOWN")
    service = PublicVideoService(adapters=())

    result = asyncio.run(
        service.read_transcript(user.id, video_id="not-inspected", offset=0)
    )

    assert result["ok"] is False
    assert result["reason_code"] == "video_not_inspected"
