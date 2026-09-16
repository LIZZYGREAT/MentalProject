"""Stable contracts for public-video metadata and transcript evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol


VideoExtractionMode = Literal[
    "public_subtitle",
    "metadata_only",
    "no_public_subtitle",
]


VIDEO_REASON_CODES = frozenset(
    {
        "unsupported_video_provider",
        "video_metadata_unavailable",
        "video_page_not_public",
        "no_public_subtitle",
        "subtitle_fetch_failed",
        "subtitle_parse_failed",
        "transcript_too_large",
        "video_url_not_safe",
        "unsupported_video_variant",
        "video_disabled",
        "video_not_inspected",
    }
)


@dataclass(frozen=True)
class VideoMetadata:
    """Public, provider-normalized metadata for one video."""

    provider: str
    canonical_url: str
    title: str
    description: str | None
    author: str | None
    duration_seconds: int | None
    published_at: str | None
    cover_url: str | None
    video_id: str = ""


@dataclass(frozen=True)
class TranscriptSegment:
    """One normalized public subtitle segment.

    Missing provider timestamps stay ``None``.  The backend never estimates
    timestamps from segment order or duration.
    """

    start_seconds: float | None
    end_seconds: float | None
    text: str


@dataclass(frozen=True)
class VideoTranscriptDocument:
    """Provider-independent transcript document used by the Agent tools."""

    video_id: str
    provider: str
    title: str
    language: str | None
    segments: tuple[TranscriptSegment, ...]
    total_chars: int
    extraction_mode: VideoExtractionMode

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)


class VideoProviderAdapter(Protocol):
    """Adapter contract for one public video provider."""

    provider: str

    def can_handle(self, url: str) -> bool:
        """Return whether this adapter owns the URL shape."""

    async def resolve_metadata(self, url: str) -> VideoMetadata:
        """Resolve a public URL into normalized metadata."""

    async def resolve_transcript(
        self, metadata: VideoMetadata
    ) -> VideoTranscriptDocument:
        """Resolve the provider's publicly readable subtitle, if any."""


class VideoProviderError(RuntimeError):
    """Stable backend error raised by a provider adapter."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        super().__init__(message or reason_code)
        self.reason_code = str(reason_code)


def metadata_as_dict(metadata: VideoMetadata) -> dict[str, Any]:
    """Return only the public normalized metadata fields."""

    return asdict(metadata)


def transcript_as_dict(document: VideoTranscriptDocument) -> dict[str, Any]:
    """Return a JSON-friendly representation for cache/test boundaries."""

    return {
        "video_id": document.video_id,
        "provider": document.provider,
        "title": document.title,
        "language": document.language,
        "segments": [asdict(segment) for segment in document.segments],
        "total_chars": document.total_chars,
        "extraction_mode": document.extraction_mode,
    }


def transcript_from_dict(value: dict[str, Any]) -> VideoTranscriptDocument:
    """Rehydrate a cached transcript after validating its basic shape."""

    segments = tuple(
        TranscriptSegment(
            start_seconds=(
                float(item["start_seconds"])
                if item.get("start_seconds") is not None
                else None
            ),
            end_seconds=(
                float(item["end_seconds"])
                if item.get("end_seconds") is not None
                else None
            ),
            text=str(item.get("text") or ""),
        )
        for item in value.get("segments", [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    )
    mode = str(value.get("extraction_mode") or "no_public_subtitle")
    if mode not in {"public_subtitle", "metadata_only", "no_public_subtitle"}:
        mode = "metadata_only"
    return VideoTranscriptDocument(
        video_id=str(value.get("video_id") or ""),
        provider=str(value.get("provider") or ""),
        title=str(value.get("title") or ""),
        language=(str(value["language"]) if value.get("language") else None),
        segments=segments,
        total_chars=int(value.get("total_chars") or sum(len(s.text) for s in segments)),
        extraction_mode=mode,  # type: ignore[arg-type]
    )
