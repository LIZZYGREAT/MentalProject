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
        transcript_max_chars: int = 120_000,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.public_transport = public_transport
        self.api_base_url = validate_public_https_url(api_base_url).rstrip("/")
        self.api_max_bytes = max(16 * 1024, int(api_max_bytes))
        self.transcript_max_chars = max(1_000, int(transcript_max_chars))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._metadata_payloads: dict[str, dict[str, Any]] = {}

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
        try:
            requested_url = validate_public_https_url(url)
        except PublicWebReadError as exc:
            raise VideoProviderError("video_url_not_safe") from exc
        if not self.can_handle(requested_url):
            raise VideoProviderError("unsupported_video_provider")
        final_url, page_body = await self._resolve_page(requested_url)
        try:
            final_host = (urlsplit(final_url).hostname or "").casefold().rstrip(".")
        except ValueError:
            final_host = ""
        if final_host not in _BILIBILI_HOSTS:
            raise VideoProviderError("video_page_not_public")
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
        self._metadata_payloads[bvid] = data
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
        data = self._metadata_payloads.get(metadata.video_id)
        if data is None:
            payload = await self._read_json(
                f"{self.api_base_url}/x/web-interface/view?"
                f"{urlencode({'bvid': metadata.video_id})}"
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        subtitle = data.get("subtitle") if isinstance(data, dict) else None
        entries = subtitle.get("list") if isinstance(subtitle, dict) else None
        if not isinstance(entries, list):
            raise VideoProviderError("no_public_subtitle")
        candidates = [
            item for item in entries
            if isinstance(item, dict) and str(item.get("subtitle_url") or "").strip()
        ]
        if not candidates:
            return self._empty_transcript(metadata)
        selected = min(candidates, key=self._subtitle_priority)
        raw_url = str(selected.get("subtitle_url") or "").strip()
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        try:
            fetched = await self.public_transport.fetch_public_url(
                raw_url,
                max_bytes=self.api_max_bytes,
                timeout_seconds=self.timeout_seconds,
            )
        except PublicWebReadError as exc:
            raise VideoProviderError("subtitle_fetch_failed") from exc
        try:
            payload = json.loads(
                fetched.response.body.decode("utf-8", errors="strict")
            )
            segments = self._normalize_segments(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VideoProviderError("subtitle_parse_failed") from exc
        if not segments:
            return self._empty_transcript(metadata)
        total_chars = len("\n".join(segment.text for segment in segments))
        if total_chars > self.transcript_max_chars:
            raise VideoProviderError("transcript_too_large")
        return VideoTranscriptDocument(
            video_id=metadata.video_id,
            provider=self.provider,
            title=metadata.title,
            language=self._language(selected),
            segments=tuple(segments),
            total_chars=total_chars,
            extraction_mode="public_subtitle",
        )

    @staticmethod
    def _empty_transcript(metadata: VideoMetadata) -> VideoTranscriptDocument:
        return VideoTranscriptDocument(
            video_id=metadata.video_id,
            provider="bilibili",
            title=metadata.title,
            language=None,
            segments=(),
            total_chars=0,
            extraction_mode="no_public_subtitle",
        )

    @staticmethod
    def _subtitle_priority(item: dict[str, Any]) -> tuple[int, str]:
        language = BilibiliVideoAdapter._language(item)
        priority = {"zh-CN": 0, "zh": 1, "en": 2}.get(language, 3)
        return priority, language

    @staticmethod
    def _language(item: dict[str, Any]) -> str:
        raw = str(item.get("lan") or item.get("lang") or "").strip()
        folded = raw.casefold().replace("_", "-")
        if folded in {"zh", "zh-cn", "ai-zh", "zh-hans", "zh-hans-cn"}:
            return "zh-CN"
        if folded in {"en", "en-us", "en-gb"}:
            return "en"
        return raw or "unknown"

    @staticmethod
    def _normalize_segments(payload: Any) -> list[TranscriptSegment]:
        if isinstance(payload, dict):
            raw_segments = payload.get("body") or payload.get("segments")
        else:
            raw_segments = payload
        if not isinstance(raw_segments, list):
            raise ValueError("subtitle body must be a list")
        normalized: list[tuple[int, TranscriptSegment]] = []
        for position, item in enumerate(raw_segments):
            if not isinstance(item, dict):
                continue
            text = str(
                item.get("content")
                if item.get("content") is not None
                else item.get("text") or ""
            ).strip()
            if not text:
                continue
            start = BilibiliVideoAdapter._timestamp(
                item.get("from", item.get("start_seconds", item.get("start")))
            )
            end = BilibiliVideoAdapter._timestamp(
                item.get("to", item.get("end_seconds", item.get("end")))
            )
            if start is not None and end is not None and end < start:
                start = None
                end = None
            normalized.append((
                position,
                TranscriptSegment(start_seconds=start, end_seconds=end, text=text),
            ))
        normalized.sort(
            key=lambda pair: (
                pair[1].start_seconds is None,
                pair[1].start_seconds if pair[1].start_seconds is not None else 0.0,
                pair[0],
            )
        )
        return [segment for _position, segment in normalized]

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(result) or result < 0:
            return None
        return result

    async def _resolve_page(self, url: str) -> tuple[str, str]:
        parsed = urlsplit(validate_public_https_url(url))
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host not in {"b23.tv", "www.b23.tv"}:
            return validate_public_https_url(url), ""
        try:
            fetched = await self.public_transport.fetch_public_url(url)
        except PublicWebReadError as exc:
            reason = getattr(exc, "reason_code", "")
            if reason in {
                "invalid_url",
                "invalid_url_scheme",
                "invalid_url_port",
                "url_credentials_not_allowed",
                "secret_query_not_allowed",
                "url_private_address",
                "dns_resolution_failed",
            }:
                raise VideoProviderError("video_url_not_safe") from exc
            raise VideoProviderError("video_page_not_public") from exc
        body = fetched.response.body.decode("utf-8", errors="replace")
        return fetched.canonical_url, body

    async def _read_json(self, url: str) -> dict[str, Any]:
        try:
            fetched = await self.public_transport.fetch_public_url(
                url,
                max_bytes=self.api_max_bytes,
                timeout_seconds=self.timeout_seconds,
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
