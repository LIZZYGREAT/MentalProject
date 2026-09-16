import asyncio
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry
from app.tools.video import VideoTools


class FakeService:
    async def inspect_url(self, participant_id, *, url):
        return {"ok": True, "participant": str(participant_id), "url": url}

    async def read_transcript(self, participant_id, *, video_id, offset, limit_chars):
        return {
            "ok": True,
            "participant": str(participant_id),
            "video_id": video_id,
            "offset": offset,
            "limit_chars": limit_chars,
        }


def context():
    return AgentContext(
        participant_id=uuid.uuid4(),
        participant_code="P-VIDEO",
        open_id="open",
        chat_id="chat",
        message_id="message",
        agent_run_id=uuid.uuid4(),
    )


def test_video_tools_register_closed_read_only_schemas():
    registry = ToolRegistry()
    VideoTools(FakeService()).register(registry)

    assert set(registry.names) == {"video_inspect_url", "video_read_transcript"}
    assert all(spec.effect == "read" for spec in registry.specs)
    assert all(spec.authorization_requirement == "none" for spec in registry.specs)
    assert registry.specs[0].parameters["additionalProperties"] is False


def test_video_tools_are_participant_bound_and_structured():
    registry = ToolRegistry()
    VideoTools(FakeService()).register(registry)
    ctx = context()

    inspect = asyncio.run(
        registry.execute(ctx, "video_inspect_url", {"url": "https://b23.tv/x"})
    )
    read = asyncio.run(
        registry.execute(
            ctx,
            "video_read_transcript",
            {"video_id": "BV1", "offset": 100, "limit_chars": 2000},
        )
    )

    assert inspect.status == "succeeded"
    assert inspect.result["participant"] == str(ctx.participant_id)
    assert read.status == "succeeded"
    assert read.result["offset"] == 100
