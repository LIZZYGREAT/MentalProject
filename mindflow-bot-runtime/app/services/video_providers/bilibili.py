"""Bilibili public-page metadata adapter.

Only anonymous public endpoints are used.  Authentication cookies and caller
credentials are deliberately not part of the adapter or transport contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import math
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from app.contracts.public_video import (
    TranscriptSegment,
    VideoMetadata,
    VideoProviderError,
    VideoTranscriptDocument,
)
from app.services.public_web_document_service import (
    PublicWebDocumentService,
    PublicWebReadError,
    validate_public_https_url,
)


_BILIBILI_HOSTS = frozenset(
    {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "www.b23.tv"}
)
_BVID_PATTERN = re.compile(r"(?i)(BV[0-9A-Za-z]{6,})")
_AV_PATH_PATTERN = re.compile(r"(?i)^/av(\d+)(?:/|$)")


class BilibiliVideoAdapter:
    provider = "bilibili"

    def __init__(
        self,
        public_transport: PublicWebDocumentService,
        *,
        api_base_url: str = "https://api.bilibili.com",
        api_max_bytes: int = 512 * 1024,
    ) -> None:
        self.public_transport = public_transport
        self.api_base_url = validate_public_https_url(api_base_url).rstrip("/")
        self.api_max_bytes = max(16 * 1024, int(api_max_bytes))

    def can_handle(self, url: str) -> bool:
        try:
            parsed = urlsplit(str(url or ""))
            host = (parsed.hostname or "").casefold().rstrip(".")
        except ValueError:
            return False
        if parsed.scheme.casefold() != "https" or host not in _BILIBILI_HOSTS:
            return False
        if host.endswith("b23.tv"):
            return True
        path = parsed.path.casefold()
        return "/video/" in f"{path}/" or bool(_AV_PATH_PATTERN.match(path))

    async def resolve_metadata(self, url: str) -> VideoMetadata:
        if not self.can_handle(url):
            raise VideoProviderError("unsupported_video_provider")
        final_url, page_body = await self._resolve_page(url)
        reference = self._video_reference(final_url)
        if reference is None and page_body:
            reference = self._video_reference_from_markup(page_body)
        if reference is None:
            raise VideoProviderError("video_metadata_unavailable")
        query_key, query_value = reference
        api_url = f"{self.api_base_url}/x/web-interface/view?{urlencode({query_key: query_value})}"
        payload = await self._read_json(api_url)
        code = payload.get("code")
        if code != 0 or not isinstance(payload.get("data"), dict):
            if code in { -101, -400, -403 }:
                raise VideoProviderError("video_page_not_public")
            raise VideoProviderError("video_metadata_unavailable")
        data = payload["data"]
        bvid = str(data.get("bvid") or "").strip()
        if not bvid:
            bvid = query_value if query_key == "bvid" else ""
        if not bvid:
            raise VideoProviderError("video_metadata_unavailable")
        return VideoMetadata(
            provider=self.provider,
            canonical_url=f"https://www.bilibili.com/video/{bvid}",
            title=self._clean_text(data.get("title"), fallback="Bilibili video", limit=300),
            description=self._optional_text(data.get("desc"), limit=2_000),
            author=self._owner_name(data.get("owner")),
            duration_seconds=self._duration(data.get("duration")),
            published_at=self._published_at(data.get("pubdate")),
            cover_url=self._https_url(data.get("pic")),
            video_id=bvid,
        )

    async def resolve_transcript(
        self, metadata: VideoMetadata
    ) -> VideoTranscriptDocument:
        """Part 2 placeholder; public subtitle discovery is added in Part 3."""

        return VideoTranscriptDocument(
            video_id=metadata.video_id,
            provider=self.provider,
            title=metadata.title,
            language=None,
            segments=(),
            total_chars=0,
            extraction_mode="no_public_subtitle",
        )

    async def _resolve_page(self, url: str) -> tuple[str, str]:
        parsed = urlsplit(validate_public_https_url(url))
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host not in {"b23.tv", "www.b23.tv"}:
            return validate_public_https_url(url), ""
        try:
            fetched = await self.public_transport.fetch_public_url(url)
        except PublicWebReadError as exc:
            raise VideoProviderError("video_page_not_public") from exc
        body = fetched.response.body.decode("utf-8", errors="replace")
        return fetched.canonical_url, body

    async def _read_json(self, url: str) -> dict[str, Any]:
        try:
            fetched = await self.public_transport.fetch_public_url(
                url, max_bytes=self.api_max_bytes
            )
        except PublicWebReadError as exc:
            raise VideoProviderError("video_metadata_unavailable") from exc
        try:
            payload = json.loads(
                fetched.response.body.decode("utf-8", errors="strict")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VideoProviderError("video_metadata_unavailable") from exc
        if not isinstance(payload, dict):
            raise VideoProviderError("video_metadata_unavailable")
        return payload

    @staticmethod
    def _video_reference(url: str) -> tuple[str, str] | None:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None
        bvid = _BVID_PATTERN.search(parsed.path) or _BVID_PATTERN.search(parsed.query)
        if bvid:
            return "bvid", bvid.group(1)
        av_match = _AV_PATH_PATTERN.match(parsed.path)
        if av_match:
            return "aid", av_match.group(1)
        aid = (parse_qs(parsed.query).get("aid") or [""])[0]
        if aid.isdigit() and int(aid) > 0:
            return "aid", aid
        bvid_query = (parse_qs(parsed.query).get("bvid") or [""])[0]
        if _BVID_PATTERN.fullmatch(bvid_query):
            return "bvid", bvid_query
        return None

    @staticmethod
    def _video_reference_from_markup(markup: str) -> tuple[str, str] | None:
        match = _BVID_PATTERN.search(markup)
        return ("bvid", match.group(1)) if match else None

    @staticmethod
    def _clean_text(value: Any, *, fallback: str, limit: int) -> str:
        text = html.unescape(str(value or "")).strip()
        return (text or fallback)[:limit]

    @staticmethod
    def _optional_text(value: Any, *, limit: int) -> str | None:
        text = html.unescape(str(value or "")).strip()
        return text[:limit] or None

    @staticmethod
    def _owner_name(value: Any) -> str | None:
        return BilibiliVideoAdapter._optional_text(
            value.get("name") if isinstance(value, dict) else None, limit=120
        )

    @staticmethod
    def _duration(value: Any) -> int | None:
        try:
            duration = int(value)
        except (TypeError, ValueError):
            return None
        return duration if duration >= 0 else None

    @staticmethod
    def _published_at(value: Any) -> str | None:
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            return None
        if timestamp <= 0:
            return None
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _https_url(value: Any) -> str | None:
        raw = str(value or "").strip()
        if raw.startswith("//"):
            raw = "https:" + raw
        if raw.startswith("http://"):
            raw = "https://" + raw[len("http://") :]
        if not raw.startswith("https://"):
            return None
        try:
            return validate_public_https_url(raw)
        except PublicWebReadError:
            return None
