import asyncio
import json
import uuid
from pathlib import Path

from app.agent.context import AgentContext
from app.agent.sdk_adapter import (
    DISALLOWED_TOOLS,
    SKILL_NAME,
    ClaudeSDKInvocationError,
    ProductionClaudeClient,
    ProductionClaudeClientFactory,
    isolate_process_environment,
)
from app.agent.sdk_mcp import TurnContextBinding, build_sdk_mcp_server
from app.agent.tool_registry import FORBIDDEN_FIELDS, ToolRegistry
from app.presentation.contracts import AgentActivityEvent
from app.tools.care import CareTools


class FakeSDK:
    class PermissionResultAllow:
        pass

    class PermissionResultDeny:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ClaudeSDKClient:
        def __init__(self, options):
            self.options = options

    @staticmethod
    def tool(name, description, parameters):
        def decorate(handler):
            handler.tool_name = name
            handler.description = description
            handler.parameters = parameters
            return handler

        return decorate

    @staticmethod
    def create_sdk_mcp_server(name, version, tools):
        return {"type": "sdk", "name": name, "version": version, "tools": tools}


def test_sdk_mcp_uses_registry_schema_and_backend_context_only():
    seen = []
    registry = ToolRegistry()

    async def handler(ctx, args):
        seen.append((ctx.participant_id, args))
        return {"ok": True, "value": args["value"]}

    registry.register(
        "safe_tool",
        "safe",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler,
    )
    binding = TurnContextBinding(
        AgentContext(uuid.uuid4(), "P001", "ou", "oc", "msg", uuid.uuid4())
    )
    server = build_sdk_mcp_server(registry, binding, sdk=FakeSDK)
    tool = server["tools"][0]
    response = asyncio.run(tool({"value": "ok"}))
    assert seen == [(binding.current.participant_id, {"value": "ok"})]
    assert json.loads(response["content"][0]["text"])["value"] == "ok"
    schema_text = json.dumps(tool.parameters).lower()
    assert not any(field in schema_text for field in FORBIDDEN_FIELDS)


def test_sdk_mcp_emits_one_real_start_and_success_lifecycle_event():
    activities = []
    registry = ToolRegistry()

    async def handler(_ctx, _args):
        return {"ok": True}

    registry.register(
        "safe_tool",
        "safe",
        {"type": "object", "properties": {}, "additionalProperties": False},
        handler,
    )

    async def activity(event: AgentActivityEvent):
        activities.append(event)

    binding = TurnContextBinding(
        AgentContext(uuid.uuid4(), "P001", "ou", "oc", "msg", uuid.uuid4()),
        activity_callback=activity,
    )
    tool = build_sdk_mcp_server(registry, binding, sdk=FakeSDK)["tools"][0]
    asyncio.run(tool({}))

    assert [(event.kind, event.tool_name, event.status) for event in activities] == [
        ("tool_started", "safe_tool", None),
        ("tool_succeeded", "safe_tool", "succeeded"),
    ]


def test_sdk_mcp_emits_failed_lifecycle_without_sensitive_payloads():
    activities = []
    registry = ToolRegistry()

    async def handler(_ctx, _args):
        raise RuntimeError("secret raw result")

    registry.register(
        "safe_tool",
        "safe",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler,
    )

    async def activity(event: AgentActivityEvent):
        activities.append(event)

    binding = TurnContextBinding(
        AgentContext(uuid.uuid4(), "P001", "ou", "oc", "msg", uuid.uuid4()),
        activity_callback=activity,
    )
    tool = build_sdk_mcp_server(registry, binding, sdk=FakeSDK)["tools"][0]
    response = asyncio.run(tool({"value": "private argument"}))

    assert response["is_error"] is True
    assert [(event.kind, event.tool_name, event.status) for event in activities] == [
        ("tool_started", "safe_tool", None),
        ("tool_failed", "safe_tool", "tool_exception"),
    ]
    assert all(not hasattr(event, "arguments") for event in activities)
    assert "private argument" not in repr(activities)


def test_all_production_tool_schemas_are_closed_and_identity_free():
    registry = ToolRegistry()
    CareTools(None, None, None, None, None, "Asia/Shanghai", object()).register(registry)

    assert set(registry.names) == {
        "care_get_today_context",
        "care_record_checkin",
        "care_get_recent_state",
        "care_run_today_assessment",
        "care_get_pressure_curve",
        "care_get_checkin_card",
        "care_get_support",
        "calendar_connection_status",
        "calendar_list_calendars",
        "calendar_list_events",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_delete_event",
    }
    for spec in registry.specs:
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False
        properties = set(spec.parameters.get("properties", {}))
        assert properties.isdisjoint(FORBIDDEN_FIELDS)

    delete_spec = next(
        spec for spec in registry.specs if spec.name == "calendar_delete_event"
    )
    assert delete_spec.parameters["required"] == ["event_id", "confirmed"]
    assert delete_spec.parameters["properties"]["confirmed"] == {
        "type": "boolean",
        "const": True,
    }


def test_production_options_expose_only_skill_and_mindflow_tools(monkeypatch):
    registry = ToolRegistry()
    registry.register(
        "safe_tool",
        "safe",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _ctx, _args: {"ok": True},
    )
    root = Path(__file__).resolve().parents[2] / "claude-runtime"
    factory = ProductionClaudeClientFactory(
        registry,
        workdir=root,
        plugin_path=root / "plugins" / "mindflow-care",
        settings_path=root / ".claude" / "settings.json",
        model="deepseek-primary",
        opus_model="deepseek-v4-pro",
        sonnet_model="deepseek-v4-pro",
        haiku_model="deepseek-v4-flash",
        subagent_model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/anthropic",
        auth_token="top-secret-token",
        max_turns=8,
    )
    monkeypatch.setattr("app.agent.sdk_adapter._load_sdk", lambda: FakeSDK)
    adapter = factory.create(TurnContextBinding(), resume_session_id="session-1")
    options = adapter.client.options
    assert options.tools == ["Skill"]
    assert options.skills == [SKILL_NAME]
    assert options.allowed_tools == ["mcp__mindflow__safe_tool"]
    assert set(options.disallowed_tools) == set(DISALLOWED_TOOLS)
    assert options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.plugins == [
        {"type": "local", "path": str(root / "plugins" / "mindflow-care")}
    ]
    assert options.permission_mode == "dontAsk"
    assert options.fallback_model is None
    assert options.resume == "session-1"
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == "top-secret-token"
    assert options.env["ANTHROPIC_MODEL"] == "deepseek-primary"
    assert options.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-v4-pro"
    assert options.env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "deepseek-v4-pro"
    assert options.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-v4-flash"
    assert options.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-flash"
    assert "top-secret-token" not in str(options.allowed_tools)

    assert "Bash" in options.disallowed_tools
    assert "Read" in options.disallowed_tools
    assert "Write" in options.disallowed_tools
    assert "Edit" in options.disallowed_tools
    assert "WebSearch" in options.disallowed_tools
    assert "WebFetch" in options.disallowed_tools
    assert set(options.mcp_servers) == {"mindflow"}

def test_parent_environment_is_reduced_to_runtime_allowlist():
    environment = {
        "PATH": "/usr/bin",
        "HOME": "/home/mindflow",
        "FEISHU_BOT_APP_SECRET": "feishu-secret",
        "DATABASE_URL": "postgresql://secret",
        "TOKEN_ENCRYPTION_KEY": "encryption-secret",
        "DEEPSEEK_API_KEY": "model-secret",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
    }

    isolate_process_environment(environment)

    assert environment == {"PATH": "/usr/bin", "HOME": "/home/mindflow"}


def test_client_fails_closed_when_the_required_skill_is_not_initialized():
    class SDK:
        class SystemMessage:
            def __init__(self, skills):
                self.subtype = "init"
                self.data = {"skills": skills}

        class AssistantMessage:
            pass

        class ToolUseBlock:
            pass

        class ResultMessage:
            def __init__(self):
                self.is_error = False
                self.result = "ok"
                self.session_id = "session-1"

        class ClaudeSDKClient:
            def __init__(self, options):
                self.skills = options

            async def connect(self):
                return None

            async def query(self, _text):
                return None

            async def receive_response(self):
                yield SDK.SystemMessage(self.skills)
                yield SDK.ResultMessage()

            async def disconnect(self):
                return None

    async def scenario():
        allowed = ProductionClaudeClient(SDK, [SKILL_NAME], expected_skill=SKILL_NAME)
        assert (await allowed.run_turn("hello")).text == "ok"
        denied = ProductionClaudeClient(SDK, ["other:skill"], expected_skill=SKILL_NAME)
        with __import__("pytest").raises(ClaudeSDKInvocationError):
            await denied.run_turn("hello")

    asyncio.run(scenario())
