"""Deterministic relevance, freshness and duplicate handling for evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from app.contracts.research import ResearchEvidenceItem


_WORDS = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_QUALITY = {"github": 1.0, "paper": 1.0, "api": 0.9, "web": 0.7}


def _tokens(value: str) -> set[str]:
    return {item.casefold() for item in _WORDS.findall(value) if len(item) > 1}


def canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"), parsed.query, ""))


class MorningBriefResearchRanker:
    def __init__(self, *, max_per_topic: int = 3, max_total: int = 10) -> None:
        self.max_per_topic = max(1, min(int(max_per_topic), 5))
        self.max_total = max(1, min(int(max_total), 30))

    def rank(
        self,
        items: Iterable[ResearchEvidenceItem | dict[str, Any]],
        *,
        topic_label: str | None = None,
        topic_priority: int = 0,
        now: datetime | None = None,
        max_items: int | None = None,
    ) -> list[ResearchEvidenceItem]:
        instant = now or datetime.now(timezone.utc)
        normalized: list[ResearchEvidenceItem] = []
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        for raw in items:
            item = raw if isinstance(raw, ResearchEvidenceItem) else ResearchEvidenceItem.from_mapping(raw, topic_label=topic_label)
            if not item.verified_public_source or not item.canonical_url.startswith("https://"):
                continue
            if canonical_url(item.canonical_url) in seen_urls or item.content_hash in seen_hashes:
                continue
            seen_urls.add(canonical_url(item.canonical_url))
            seen_hashes.add(item.content_hash)
            score = self._score(item, topic_label=topic_label, topic_priority=topic_priority, now=instant)
            if score <= 0:
                continue
            normalized.append(ResearchEvidenceItem(**(item.as_dict() | {"rank_score": score})))
        normalized.sort(key=lambda item: (-float(item.rank_score or 0), item.title.casefold()))
        limit = min(self.max_total, max_items or self.max_per_topic)
        return normalized[:limit]

    @staticmethod
    def _score(item: ResearchEvidenceItem, *, topic_label: str | None, topic_priority: int, now: datetime) -> float:
        relevance = 1.0
        if topic_label:
            terms = _tokens(topic_label)
            evidence_terms = _tokens(f"{item.title} {item.content[:2000]}")
            relevance = 0.5 + (len(terms & evidence_terms) / max(1, len(terms)))
        date_value = item.published_at or item.updated_at
        confidence = 1.0 if date_value else 0.25
        try:
            event_time = datetime.fromisoformat((date_value or item.retrieved_at).replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (now - event_time.astimezone(timezone.utc)).total_seconds() / 3600)
        except ValueError:
            age_hours = float(item.freshness_hours) + 1
            confidence = 0.0
        freshness = confidence * max(0.0, 1.0 - age_hours / max(1, item.freshness_hours))
        if freshness <= 0:
            return 0.0
        quality = _QUALITY.get(item.source_kind, 0.5)
        return round(relevance * 0.45 + freshness * 0.30 + quality * 0.20 + max(0, min(topic_priority, 10)) * 0.005, 6)
