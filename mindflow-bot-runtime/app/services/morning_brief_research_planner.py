"""Turn confirmed topic preferences into bounded public research jobs."""

from __future__ import annotations

from typing import Any, Iterable

from app.contracts.research import MorningBriefTopic, ResearchJobSpec


class MorningBriefResearchPlanner:
    def __init__(self, *, max_topics: int = 10) -> None:
        self.max_topics = max(1, min(int(max_topics), 20))

    def plan(
        self,
        topics: Iterable[MorningBriefTopic | dict[str, Any]],
        *,
        lookback_hours: int = 24,
        language: str = "zh-CN",
        max_research_items: int = 10,
    ) -> list[ResearchJobSpec]:
        if not 1 <= int(lookback_hours) <= 24 * 30:
            raise ValueError("lookback_hours is outside the allowed range")
        jobs: list[ResearchJobSpec] = []
        for raw in list(topics)[: self.max_topics]:
            topic = raw if isinstance(raw, MorningBriefTopic) else MorningBriefTopic(
                id=str(raw.get("id")) if raw.get("id") else None,
                participant_id=None,
                topic_label=str(raw.get("topic_label") or ""),
                query_hints=tuple(raw.get("query_hints") or raw.get("query_hints_json") or ()),
                source_kinds=tuple(raw.get("source_kinds") or raw.get("source_kinds_json") or ("web",)),
                priority=int(raw.get("priority", 0)),
                enabled=bool(raw.get("enabled", True)),
            )
            if not topic.enabled:
                continue
            jobs.append(ResearchJobSpec(
                topic=topic.topic_label,
                query_hints=topic.query_hints,
                freshness_hours=int(lookback_hours),
                language=language,
                source_kinds=topic.source_kinds,
                max_searches=min(4, max(1, max_research_items // 2)),
                max_pages=min(12, max(1, max_research_items + 2)),
                max_browser_pages=3,
                max_exec_calls=3,
                deadline_seconds=90,
            ))
        return jobs

