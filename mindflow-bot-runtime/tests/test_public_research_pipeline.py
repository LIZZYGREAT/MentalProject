import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace

from app.contracts.research import ResearchEvidenceItem
from app.repositories_morning_brief_preferences import MorningBriefTopicRepository
from app.services.morning_brief_composer import MorningBriefComposer
from app.services.morning_brief_research_planner import MorningBriefResearchPlanner
from app.services.research_ranker import MorningBriefResearchRanker
from app.services.public_research_service import PublicResearchService
from tests.helpers import memory_database, participant


def test_confirmed_topic_repository_keeps_topics_separate_from_care_preferences():
    database = memory_database()
    user = participant(database, "BRIEF-TOPIC-1")
    repository = MorningBriefTopicRepository(database)
    assert repository.get_preferences(user.id)["include_calendar"] is True
    topic = repository.add_topic(
        user.id,
        topic_label="具身智能",
        query_hints=["embodied AI", "robot learning"],
        source_kinds=["web", "paper"],
        priority=3,
    )
    assert topic["topic_label"] == "具身智能"
    assert repository.list_topics(user.id)[0]["source_kinds"] == ["web", "paper"]
    removed = repository.remove_topic(user.id, "具身智能")
    assert removed["ok"] is True
    assert repository.list_topics(user.id) == []


def test_planner_never_receives_or_emits_participant_context():
    jobs = MorningBriefResearchPlanner().plan([
        {"topic_label": "游戏行业", "query_hints": ["game industry"], "source_kinds": ["web"]},
        {"topic_label": "停用主题", "enabled": False},
    ])
    assert len(jobs) == 1
    payload = jobs[0].as_public_payload()
    assert payload["topic"] == "游戏行业"
    assert "participant_id" not in payload
    assert "calendar" not in payload


def test_ranker_deduplicates_and_caps_items():
    now = datetime.now(timezone.utc)
    first = ResearchEvidenceItem.build(
        source_kind="github", title="Agent toolkit", canonical_url="https://github.com/a/b#readme",
        content="agent project", extraction_mode="api", freshness_hours=24,
    )
    second = ResearchEvidenceItem.build(
        source_kind="web", title="Same event", canonical_url="https://news.example/event",
        content="agent project", extraction_mode="http", freshness_hours=24,
    )
    ranked = MorningBriefResearchRanker().rank([first, second, first], topic_label="Agent", now=now)
    assert len(ranked) == 1
    assert ranked[0].source_kind == "github"


def test_composer_contains_provenance_and_degrades_without_research():
    item = ResearchEvidenceItem.build(
        source_kind="paper", title="New paper", canonical_url="https://arxiv.org/abs/1",
        content="evidence", extraction_mode="api", freshness_hours=24,
        publisher="arXiv",
    )
    message = MorningBriefComposer().compose(
        "2026-09-18", [], [], {"科学研究": [item]}
    )
    assert "New paper" in message
    assert item.evidence_id in message
    assert "压力预测" not in message

    fallback = MorningBriefComposer().compose(
        "2026-09-18", [{"summary": "课程"}], [], {"科学研究": []}, research_unavailable=True
    )
    assert "公开信息部分今天暂时没有完成更新" in fallback


def test_search_candidate_is_opened_before_becoming_evidence():
    calls = []

    class Gateway:
        async def search(self, job):
            return {"ok": True, "candidate_only": True, "results": [{
                "candidate_id": "c1", "source_kind": "web", "title": "Article",
                "url": "https://example.com/article",
            }]}

        async def open_url(self, job, url):
            calls.append(url)
            item = ResearchEvidenceItem.build(
                source_kind="web", title="Article", canonical_url=url,
                content="The article explains a meaningful change.", extraction_mode="http", freshness_hours=24,
            )
            return {"ok": True, "evidence": item.as_dict()}

        async def browser_open(self, *_args):
            raise AssertionError("browser fallback should not be needed")

        async def exec_public(self, *_args):
            raise AssertionError

        async def github(self, *_args):
            raise AssertionError

    result = asyncio.run(PublicResearchService(
        gateway=Gateway(), web_search=None, web_documents=None,
    ).search(None, topic="Agent"))
    assert calls == ["https://example.com/article"]
    assert result["verified"] is True
    assert result["results"][0]["content"]


def test_ranker_uses_published_time_not_retrieved_time_for_freshness():
    item = ResearchEvidenceItem.build(
        source_kind="web", title="Old article", canonical_url="https://example.com/old",
        content="old", extraction_mode="http", freshness_hours=24,
    )
    old = replace(item, published_at="2020-01-01T00:00:00+00:00")
    assert MorningBriefResearchRanker().rank(
        [old], topic_label="article", now=datetime(2026, 9, 18, tzinfo=timezone.utc)
    ) == []
