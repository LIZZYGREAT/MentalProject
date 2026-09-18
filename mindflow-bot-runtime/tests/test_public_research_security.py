from pathlib import Path

from app.repositories_morning_brief_preferences import MorningBriefTopicRepository
from app.repositories_personalization_proposal import PersonalizationProposalRepository
from app.services.personalization_proposal_service import PersonalizationProposalService
from tests.helpers import memory_database, participant


def test_topic_subscription_requires_review_before_persistence():
    database = memory_database()
    user = participant(database, "BRIEF-PROPOSAL-1")
    topics = MorningBriefTopicRepository(database)
    proposals = PersonalizationProposalRepository(database)
    service = PersonalizationProposalService(
        proposals, None, None, morning_brief_topics=topics
    )

    staged = service.stage_morning_brief_topic(
        user.id,
        operation="add",
        topic_label="GPU 芯片",
        source_kinds=["web", "github"],
    )
    assert topics.list_topics(user.id) == []
    assert staged["domain"] == "morning_brief_topics"
    confirmed = service.resolve(user.id, staged["id"], confirmed=True)
    assert confirmed["ok"] is True
    assert topics.list_topics(user.id)[0]["topic_label"] == "GPU 芯片"


def test_compose_does_not_expose_private_runtime_inputs_or_mounts():
    compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(encoding="utf-8")
    runtime = compose.split("\n  research-runtime:\n", 1)[1].split("\n  acceptance:", 1)[0]
    assert "env_file" not in runtime
    assert "postgres_data" not in runtime
    assert "claude_state" not in runtime
    assert "research_workspace:/workspace" in runtime
    assert "cap_drop:" in runtime and "- ALL" in runtime
    assert "no-new-privileges:true" in runtime
    assert "internal: true" in compose
