import asyncio

from app.contracts.public_video import (
    TranscriptSegment,
    VideoMetadata,
    VideoTranscriptDocument,
    metadata_as_dict,
    transcript_as_dict,
    transcript_from_dict,
)
from app.services.public_video_service import PublicVideoService


class FakeVideoAdapter:
    provider = "fake"

    def can_handle(self, url):
        return url.startswith("https://video.example/")

    async def resolve_metadata(self, url):
        return VideoMetadata(
            provider=self.provider,
            canonical_url=url,
            title="Demo video",
            description="A public demo",
            author="Author",
            duration_seconds=12,
            published_at=None,
            cover_url=None,
            video_id="demo-1",
        )

    async def resolve_transcript(self, metadata):
        return VideoTranscriptDocument(
            video_id=metadata.video_id,
            provider=metadata.provider,
            title=metadata.title,
            language="en",
            segments=(TranscriptSegment(None, None, "hello"),),
            total_chars=5,
            extraction_mode="public_subtitle",
        )


def test_contract_round_trip_keeps_missing_timestamps_missing():
    document = FakeVideoAdapter().resolve_transcript
    segment = TranscriptSegment(None, None, "untrusted text")
    value = transcript_as_dict(
        VideoTranscriptDocument(
            video_id="v1",
            provider="fake",
            title="Title",
            language=None,
            segments=(segment,),
            total_chars=len(segment.text),
            extraction_mode="public_subtitle",
        )
    )

    restored = transcript_from_dict(value)

    assert restored.segments == (segment,)
    assert restored.text == "untrusted text"
    assert restored.segments[0].start_seconds is None
    assert metadata_as_dict(
        VideoMetadata("fake", "https://video.example/v1", "Title", None, None, None, None, None)
    )["provider"] == "fake"


def test_service_selects_adapter_without_natural_language_routing():
    service = PublicVideoService(adapters=(FakeVideoAdapter(),))

    result = asyncio.run(
        service.inspect_url("participant-1", url="https://video.example/demo")
    )

    assert result["ok"] is True
    assert result["video_id"] == "demo-1"
    assert result["transcript_available"] is True


def test_service_rejects_unsupported_provider():
    service = PublicVideoService(adapters=(FakeVideoAdapter(),))

    result = asyncio.run(
        service.inspect_url("participant-1", url="https://other.example/video")
    )

    assert result["ok"] is False
    assert result["reason_code"] == "unsupported_video_provider"
