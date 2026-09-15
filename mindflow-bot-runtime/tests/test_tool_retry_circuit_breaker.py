import asyncio
import uuid

from app.agent.context import AgentContext
from app.agent.tool_registry import ToolRegistry


def _context():
    return AgentContext(uuid.uuid4(), "P", "open", "chat", "message", uuid.uuid4())


def _registry(handler):
    registry = ToolRegistry()
    registry.register(
        "bounded_read",
        "Test bounded read.",
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler,
        effect="read",
        authorization_requirement="none",
    )
    return registry


def test_non_retryable_identical_tool_failure_is_short_circuited():
    calls = []

    async def handler(_ctx, args):
        calls.append(dict(args))
        return {
            "ok": False,
            "error": "calendar_range_too_large",
            "reason_code": "calendar_range_too_large",
            "retryable": False,
            "do_not_retry": True,
        }

    registry = _registry(handler)
    ctx = _context()
    first = asyncio.run(registry.execute(ctx, "bounded_read", {"value": 181}))
    second = asyncio.run(registry.execute(ctx, "bounded_read", {"value": 181}))

    assert first.result["error"] == "calendar_range_too_large"
    assert second.result == {
        "ok": False,
        "error": "repeated_non_retryable_tool_call",
        "reason_code": "calendar_range_too_large",
        "retryable": False,
        "do_not_retry": True,
    }
    assert calls == [{"value": 181}]


def test_retryable_provider_failure_is_not_permanently_blocked():
    calls = []

    async def handler(_ctx, args):
        calls.append(dict(args))
        return {
            "ok": False,
            "error": "provider_timeout",
            "retryable": True,
        }

    registry = _registry(handler)
    ctx = _context()
    first = asyncio.run(registry.execute(ctx, "bounded_read", {"value": 1}))
    second = asyncio.run(registry.execute(ctx, "bounded_read", {"value": 1}))

    assert first.result["error"] == "provider_timeout"
    assert second.result["error"] == "provider_timeout"
    assert len(calls) == 2


def test_non_retryable_failure_key_includes_arguments_and_agent_run():
    calls = []

    async def handler(_ctx, args):
        calls.append(dict(args))
        return {"ok": False, "error": "invalid_arguments", "retryable": False}

    registry = _registry(handler)
    first_ctx = _context()
    second_ctx = _context()
    asyncio.run(registry.execute(first_ctx, "bounded_read", {"value": 1}))
    different_args = asyncio.run(
        registry.execute(first_ctx, "bounded_read", {"value": 2})
    )
    different_run = asyncio.run(
        registry.execute(second_ctx, "bounded_read", {"value": 1})
    )

    assert different_args.result["error"] == "invalid_arguments"
    assert different_run.result["error"] == "invalid_arguments"
    assert len(calls) == 3
