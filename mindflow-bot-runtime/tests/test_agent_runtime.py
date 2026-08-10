import asyncio
import uuid

from app.agent.context import AgentContext
from app.agent.runtime import AgentRuntime, FALLBACK_MAX_STEPS, FALLBACK_TEMPORARY
from app.agent.skill_loader import SkillLoader
from app.agent.tool_registry import ToolRegistry
from app.integrations.deepseek import ChatResponse, DeepSeekTransientError, ToolCall
from app.repositories import ConversationRepository
from app.services.safety_service import FIXED_HIGH_RISK_RESPONSE, SafetyService
from helpers import memory_database, participant, skill_path


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, **_kwargs):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def text_response(text):
    return ChatResponse(text, (), {"role": "assistant", "content": text})


def tool_response(name, arguments):
    return ChatResponse(
        "",
        (ToolCall("call-1", name, arguments),),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": name, "arguments": "{}"}}
            ],
        },
    )


def build_runtime(client, database, registry, **overrides):
    return AgentRuntime(
        client,
        SkillLoader(skill_path()),
        registry,
        ConversationRepository(database),
        SafetyService(),
        history_limit=16,
        max_tool_steps=overrides.get("max_tool_steps", 4),
        timeout_seconds=overrides.get("timeout_seconds", 1),
        max_retries=overrides.get("max_retries", 1),
    )


def context(participant_id, marker="1"):
    return AgentContext(
        participant_id,
        f"P00{marker}",
        f"ou_{marker}",
        f"oc_{marker}",
        f"msg_{marker}",
        uuid.uuid4(),
    )


def test_tool_loop_invalid_tool_args_exception_and_final_answer():
    database = memory_database()
    p1 = participant(database, "P001")
    registry = ToolRegistry()

    def exploding(_ctx, _args):
        raise RuntimeError("private traceback must not escape")

    registry.register(
        "explode",
        "test",
        {"type": "object", "properties": {}, "additionalProperties": False},
        exploding,
    )
    client = FakeClient(
        [
            tool_response("missing", {}),
            tool_response("explode", {"participant_id": "P002"}),
            tool_response("explode", {}),
            text_response("模型结果提示：请先休息一下。"),
        ]
    )
    answer = asyncio.run(build_runtime(client, database, registry).handle_message(context(p1.id), "help"))
    assert "模型结果提示" in answer
    assert client.calls == 4


def test_transient_retry_timeout_max_steps_empty_and_high_risk_fallbacks():
    database = memory_database()
    p1 = participant(database, "P001")
    registry = ToolRegistry()
    retry_client = FakeClient([DeepSeekTransientError("429"), text_response("ok")])
    assert asyncio.run(
        build_runtime(retry_client, database, registry).handle_message(context(p1.id), "hello")
    ) == "ok"
    assert retry_client.calls == 2

    empty_client = FakeClient([text_response("")])
    assert "暂时无法" in asyncio.run(
        build_runtime(empty_client, database, registry).handle_message(context(p1.id, "2"), "empty")
    )

    loop_client = FakeClient([tool_response("missing", {}), tool_response("missing", {})])
    assert asyncio.run(
        build_runtime(loop_client, database, registry, max_tool_steps=2).handle_message(
            context(p1.id, "3"), "loop"
        )
    ) == FALLBACK_MAX_STEPS

    async def timeout_case():
        class Slow:
            calls = 0

            async def chat(self, **_kwargs):
                self.calls += 1
                await asyncio.sleep(0.05)
                return text_response("late")

        slow = Slow()
        result = await build_runtime(
            slow, database, registry, timeout_seconds=0.001, max_retries=0
        ).handle_message(context(p1.id, "4"), "slow")
        return result

    assert asyncio.run(timeout_case()) == FALLBACK_TEMPORARY

    high_risk_client = FakeClient([text_response("must not run")])
    result = asyncio.run(
        build_runtime(high_risk_client, database, registry).handle_message(
            context(p1.id, "5"), "我想自杀"
        )
    )
    assert result == FIXED_HIGH_RISK_RESPONSE
    assert high_risk_client.calls == 0


def test_timeout_applies_to_the_whole_agent_run():
    database = memory_database()
    p1 = participant(database, "P001")
    registry = ToolRegistry()

    async def scenario():
        runtime = build_runtime(
            FakeClient([]),
            database,
            registry,
            timeout_seconds=0.01,
            max_retries=0,
            max_tool_steps=4,
        )

        async def blocked_run(_ctx, _text, *, chat_type):
            await asyncio.Event().wait()

        runtime._handle_message = blocked_run
        return await runtime.handle_message(context(p1.id), "slow loop")

    assert asyncio.run(scenario()) == FALLBACK_TEMPORARY


def test_conversation_history_cannot_cross_participants():
    database = memory_database()
    p1 = participant(database, "P001")
    p2 = participant(database, "P002")
    conversations = ConversationRepository(database)
    conversations.add(p1.id, "user", "apple")
    conversations.add(p2.id, "user", "banana")
    registry = ToolRegistry()

    class InspectClient:
        async def chat(self, *, messages, tools):
            content = " ".join(str(item.get("content")) for item in messages)
            assert "apple" in content
            assert "banana" not in content
            return text_response("isolated")

    result = asyncio.run(
        build_runtime(InspectClient(), database, registry).handle_message(
            context(p1.id), "what did I say?"
        )
    )
    assert result == "isolated"
