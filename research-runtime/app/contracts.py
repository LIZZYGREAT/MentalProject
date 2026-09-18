"""Validated contracts for the public-only research runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


MAX_TOPIC_CHARS = 240
MAX_QUERY_CHARS = 500
MAX_HINTS = 8
MAX_SOURCE_KINDS = 6
ALLOWED_SOURCE_KINDS = frozenset({"web", "github", "paper", "api"})
_PRIVATE_FIELDS = frozenset({
    "participant_id", "open_id", "student_no", "memory", "psychological_context",
    "pressure_curve", "calendar", "chat_history", "user_message", "cookies",
    "authorization", "token", "secret",
})


def _text(value: Any, *, name: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = " ".join(value.split())
    if required and not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{name} is too long")
    return value


def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class ResearchJobSpec:
    topic: str
    query_hints: tuple[str, ...] = ()
    freshness_hours: int = 24
    language: str = "zh-CN"
    source_kinds: tuple[str, ...] = ("web",)
    max_searches: int = 4
    max_pages: int = 12
    max_browser_pages: int = 3
    max_exec_calls: int = 3
    deadline_seconds: int = 90

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResearchJobSpec":
        unknown = _PRIVATE_FIELDS.intersection(str(key).casefold() for key in raw)
        if unknown:
            raise ValueError("private research context is not accepted")
        topic = _text(raw.get("topic"), name="topic", limit=MAX_TOPIC_CHARS)
        hints_raw = raw.get("query_hints", [])
        if not isinstance(hints_raw, list) or len(hints_raw) > MAX_HINTS:
            raise ValueError("query_hints must be a bounded list")
        hints = tuple(_text(item, name="query_hint", limit=MAX_QUERY_CHARS) for item in hints_raw)
        language = _text(raw.get("language", "zh-CN"), name="language", limit=32)
        source_raw = raw.get("source_kinds", ["web"])
        if not isinstance(source_raw, list) or not source_raw or len(source_raw) > MAX_SOURCE_KINDS:
            raise ValueError("source_kinds must be a bounded non-empty list")
        source_kinds = tuple(dict.fromkeys(str(item).casefold() for item in source_raw))
        if not set(source_kinds).issubset(ALLOWED_SOURCE_KINDS):
            raise ValueError("unsupported source kind")
        return cls(
            topic=topic,
            query_hints=hints,
            freshness_hours=_bounded_int(raw.get("freshness_hours", 24), name="freshness_hours", minimum=1, maximum=24 * 30),
            language=language,
            source_kinds=source_kinds,
            max_searches=_bounded_int(raw.get("max_searches", 4), name="max_searches", minimum=1, maximum=10),
            max_pages=_bounded_int(raw.get("max_pages", 12), name="max_pages", minimum=1, maximum=50),
            max_browser_pages=_bounded_int(raw.get("max_browser_pages", 3), name="max_browser_pages", minimum=0, maximum=10),
            max_exec_calls=_bounded_int(raw.get("max_exec_calls", 3), name="max_exec_calls", minimum=0, maximum=10),
            deadline_seconds=_bounded_int(raw.get("deadline_seconds", 90), name="deadline_seconds", minimum=1, maximum=300),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "query_hints": list(self.query_hints),
            "source_kinds": list(self.source_kinds),
        }


@dataclass(frozen=True)
class ResearchEvidenceItem:
    evidence_id: str
    source_kind: str
    title: str
    canonical_url: str
    publisher: str | None
    published_at: str | None
    retrieved_at: str
    content: str
    content_hash: str
    extraction_mode: str
    freshness_hours: int
    verified_public_source: bool = True

    @classmethod
    def build(
        cls,
        *,
        source_kind: str,
        title: str,
        canonical_url: str,
        content: str,
        extraction_mode: str,
        freshness_hours: int,
        publisher: str | None = None,
        published_at: str | None = None,
    ) -> "ResearchEvidenceItem":
        normalized_url = canonicalize_url(canonical_url)
        digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        evidence_id = hashlib.sha256(
            f"{source_kind}|{normalized_url}|{digest}".encode("utf-8")
        ).hexdigest()[:32]
        return cls(
            evidence_id=evidence_id,
            source_kind=source_kind,
            title=" ".join(str(title).split())[:300],
            canonical_url=normalized_url,
            publisher=(" ".join(publisher.split())[:160] if publisher else None),
            published_at=published_at,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            content=content[:60000],
            content_hash=digest,
            extraction_mode=extraction_mode,
            freshness_hours=freshness_hours,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError("only public HTTPS URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed")
    return parsed._replace(
        scheme="https", fragment="", netloc=parsed.netloc.casefold()
    ).geturl()


def evidence_envelope(items: list[ResearchEvidenceItem]) -> str:
    """Render evidence as data, making its non-authoritative boundary explicit."""

    import json

    payload = [item.as_dict() for item in items]
    return "<external_research_evidence>\n" + json.dumps(
        {"untrusted_public_evidence_only": True, "items": payload},
        ensure_ascii=False,
    ) + "\n</external_research_evidence>"

