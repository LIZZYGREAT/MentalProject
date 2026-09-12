from datetime import datetime, timezone
import asyncio
import uuid

from app.agent.context import AgentContext
from app.agent.sdk_mcp import TurnContextBinding, build_sdk_mcp_server
from app.agent.tool_registry import ToolRegistry
from app.repositories import ObservationRepository
from app.services.research_aggregate_service import ResearchAggregateService
from app.tools.research import RESEARCH_SCOPE, ResearchTools
from helpers import memory_database, participant


class _FakeSDK:
    @staticmethod
    def tool(name, description, parameters):
        def decorate(handler):
            handler.tool_name = name
            return handler
        return decorate

    @staticmethod
    def create_sdk_mcp_server(name, version, tools):
        return {"tools": tools}


def _context(*, researcher: bool) -> AgentContext:
    return AgentContext(
        participant_id=uuid.uuid4(),
        participant_code="R001" if researcher else "P001",
        open_id="open",
        chat_id="chat",
        message_id="message",
        agent_run_id=uuid.uuid4(),
        access_tier="researcher" if researcher else "participant",
        scopes=(RESEARCH_SCOPE,) if researcher else (),
    )


def test_research_tools_are_hidden_without_tier_and_scope_and_denied_if_guessed():
    registry = ToolRegistry()
    ResearchTools(ResearchAggregateService(memory_database())).register(registry)
    participant_ctx = _context(researcher=False)
    researcher_ctx = _context(researcher=True)

    assert registry.names_for(participant_ctx) == ()
    assert len(registry.names_for(researcher_ctx)) == 4
    assert all(name.startswith("research_") for name in registry.names_for(researcher_ctx))
    participant_server = build_sdk_mcp_server(
        registry, TurnContextBinding(participant_ctx), sdk=_FakeSDK
    )
    researcher_server = build_sdk_mcp_server(
        registry, TurnContextBinding(researcher_ctx), sdk=_FakeSDK
    )
    assert participant_server["tools"] == []
    assert len(researcher_server["tools"]) == 4

def test_guessed_research_tool_is_rejected_by_backend_authorization():
    registry = ToolRegistry()
    ResearchTools(ResearchAggregateService(memory_database())).register(registry)

    result = asyncio.run(
        registry.execute(
            _context(researcher=False),
            "research_get_weekly_stress_summary",
            {"date_start": "2026-09-01", "date_end": "2026-09-13"},
        )
    )

    assert result.status == "tool_not_authorized"
    assert result.result["reason_code"] == "required_scope_missing"


def test_stress_aggregate_is_deidentified_and_small_cohorts_are_suppressed():
    database = memory_database()
    observations = ObservationRepository(database)
    people = [participant(database, f"P{i:03d}") for i in range(5)]
    for index, person in enumerate(people):
        observations.add(
            person.id,
            "ema_checkin",
            {"stress_0_10": index + 3, "private_note": f"secret-{index}"},
            observed_at=datetime(2026, 9, 10, 8, tzinfo=timezone.utc),
        )
    service = ResearchAggregateService(database, minimum_cohort_size=5)

    aggregate = service.weekly_stress_summary("2026-09-08", "2026-09-13")

    assert aggregate["suppressed"] is False
    assert aggregate["cohort_size"] == 5
    assert aggregate["observation_count"] == 5
    serialized = str(aggregate).lower()
    assert "participant" not in serialized
    assert "private_note" not in serialized
    assert "secret-" not in serialized

    suppressed = service.weekly_stress_summary("2026-09-11", "2026-09-13")
    assert suppressed == {
        "date_start": "2026-09-11",
        "date_end": "2026-09-13",
        "suppressed": True,
        "reason": "small_cohort",
        "minimum_cohort_size": 5,
    }
