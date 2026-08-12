"""Thin production adapter around ClaudeSDKClient.

The Agent SDK owns the model/tool loop. This module only locks its production
configuration, translates streamed lifecycle messages, and normalizes errors.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, MutableMapping, Protocol

from app.agent.sdk_mcp import TurnContextBinding, build_sdk_mcp_server
from app.agent.tool_registry import ToolRegistry


logger = logging.getLogger(__name__)

SKILL_PLUGIN_NAME = "mindflow-care"
SKILL_NAME = f"{SKILL_PLUGIN_NAME}:mental-health-care"

# The SDK merges options.env into os.environ rather than replacing the parent
# environment. Reduce the parent once, before any Claude process is created, so
# app/database/OAuth secrets cannot be inherited by the bundled CLI.
SAFE_PARENT_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
    }
)

DISALLOWED_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Agent",
    "Task",
    "TaskOutput",
    "TaskStop",
)

SYSTEM_RULES = """You are MindFlow, a brief non-clinical daily-care assistant.
Backend-provided identity is authoritative. Never request, infer, repeat, or change it.
Use only the mental-health-care skill and mindflow MCP facts. Never diagnose.
Keep observations distinct from predictions. Tool failure is not permission to use another channel.
Never request secrets, tokens, SQL, file paths, shell commands, or arbitrary URLs.
Final responses must be concise, calm, optional, and suitable for a private Feishu chat."""

ToolProgressCallback = Callable[[str], Awaitable[None]]


class ClaudeSDKUnavailable(RuntimeError):
    pass


class ClaudeSDKInvocationError(RuntimeError):
    pass


class ClaudeSDKTurnInterrupted(ClaudeSDKInvocationError):
    pass


@dataclass(frozen=True)
class ClaudeTurnResult:
    text: str
    session_id: str


class ClaudeClient(Protocol):
    async def connect(self) -> None: ...

    async def run_turn(
        self, text: str, on_tool_use: ToolProgressCallback | None = None
    ) -> ClaudeTurnResult: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


class ClaudeClientFactory(Protocol):
    def validate(self) -> None: ...

    def create(
        self, binding: TurnContextBinding, *, resume_session_id: str | None
    ) -> ClaudeClient: ...


def _load_sdk():
    try:
        return importlib.import_module("claude_agent_sdk")
    except ModuleNotFoundError as exc:
        raise ClaudeSDKUnavailable(
            "claude-agent-sdk is required; install the pinned requirements"
        ) from exc


def isolate_process_environment(
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Keep only non-secret process variables required by the Claude CLI."""

    target = os.environ if environment is None else environment
    safe = {
        name: value
        for name, value in target.items()
        if name.upper() in SAFE_PARENT_ENVIRONMENT
    }
    target.clear()
    target.update(safe)


def _safe_stderr(line: str) -> None:
    value = str(line).strip()
    if value:
        logger.warning(
            "claude_sdk_stderr",
            extra={"error_type": "ClaudeCLIStderr", "line_length": len(value)},
        )


class ProductionClaudeClient:
    def __init__(self, sdk, options, *, expected_skill: str):
        self.sdk = sdk
        self.client = sdk.ClaudeSDKClient(options=options)
        self.expected_skill = expected_skill
        self._capabilities_verified = False
        self._interrupted = False

    async def connect(self) -> None:
        try:
            await self.client.connect()
        except Exception as exc:
            raise ClaudeSDKInvocationError(type(exc).__name__) from exc

    async def run_turn(
        self, text: str, on_tool_use: ToolProgressCallback | None = None
    ) -> ClaudeTurnResult:
        self._interrupted = False
        result_message = None
        try:
            await self.client.query(text)
            async for message in self.client.receive_response():
                system_message = getattr(self.sdk, "SystemMessage", None)
                if (
                    not self._capabilities_verified
                    and system_message is not None
                    and isinstance(message, system_message)
                    and message.subtype == "init"
                ):
                    skills = message.data.get("skills", [])
                    if self.expected_skill not in skills:
                        raise ClaudeSDKInvocationError(
                            "required production Skill was not loaded"
                        )
                    self._capabilities_verified = True
                if isinstance(message, self.sdk.AssistantMessage):
                    for block in message.content:
                        if isinstance(block, self.sdk.ToolUseBlock) and on_tool_use:
                            await on_tool_use(block.name)
                if isinstance(message, self.sdk.ResultMessage):
                    result_message = message
        except Exception as exc:
            if self._interrupted:
                raise ClaudeSDKTurnInterrupted("turn interrupted") from exc
            raise ClaudeSDKInvocationError(type(exc).__name__) from exc
        if self._interrupted:
            raise ClaudeSDKTurnInterrupted("turn interrupted")
        if not self._capabilities_verified:
            raise ClaudeSDKInvocationError("missing SDK initialization metadata")
        if result_message is None:
            raise ClaudeSDKInvocationError("missing ResultMessage")
        if result_message.is_error:
            raise ClaudeSDKInvocationError(
                f"result_error:{result_message.subtype}:{result_message.api_error_status}"
            )
        answer = str(result_message.result or "").strip()
        session_id = str(result_message.session_id or "").strip()
        if not answer or not session_id:
            raise ClaudeSDKInvocationError("empty result or session_id")
        return ClaudeTurnResult(answer, session_id)

    async def interrupt(self) -> None:
        self._interrupted = True
        try:
            await self.client.interrupt()
        except Exception as exc:
            raise ClaudeSDKInvocationError(type(exc).__name__) from exc

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:
            logger.warning("claude_sdk_disconnect_failed", exc_info=True)


class ProductionClaudeClientFactory:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        workdir: Path,
        plugin_path: Path,
        settings_path: Path,
        model: str,
        opus_model: str,
        sonnet_model: str,
        haiku_model: str,
        subagent_model: str,
        base_url: str,
        auth_token: str,
        max_turns: int,
    ):
        self.registry = registry
        self.workdir = Path(workdir)
        self.plugin_path = Path(plugin_path)
        self.settings_path = Path(settings_path)
        self.model = model
        self.opus_model = opus_model
        self.sonnet_model = sonnet_model
        self.haiku_model = haiku_model
        self.subagent_model = subagent_model
        self.base_url = base_url
        self.auth_token = auth_token
        self.max_turns = max_turns

    def validate(self) -> None:
        _load_sdk()
        if not self.workdir.is_dir():
            raise ClaudeSDKUnavailable(f"Claude workdir missing: {self.workdir}")
        if not (self.plugin_path / ".claude-plugin" / "plugin.json").is_file():
            raise ClaudeSDKUnavailable(
                f"Claude Skill plugin missing: {self.plugin_path}"
            )
        if not self.settings_path.is_file():
            raise ClaudeSDKUnavailable(
                f"Claude production settings missing: {self.settings_path}"
            )

    def _environment(self) -> dict[str, str]:
        safe_names = (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TZ",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        )
        env = {name: os.environ[name] for name in safe_names if os.environ.get(name)}
        env.update(
            {
                "ANTHROPIC_AUTH_TOKEN": self.auth_token,
                "ANTHROPIC_BASE_URL": self.base_url,
                "ANTHROPIC_MODEL": self.model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": self.opus_model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": self.sonnet_model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.haiku_model,
                "CLAUDE_CODE_SUBAGENT_MODEL": self.subagent_model,
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_AGENT_SDK_CLIENT_APP": "mindflow/1.0",
            }
        )
        return env

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return tuple(f"mcp__mindflow__{name}" for name in self.registry.names)

    def create(
        self, binding: TurnContextBinding, *, resume_session_id: str | None
    ) -> ProductionClaudeClient:
        sdk = _load_sdk()
        server = build_sdk_mcp_server(self.registry, binding, sdk=sdk)

        options = sdk.ClaudeAgentOptions(
            tools=["Skill"],
            skills=[SKILL_NAME],
            allowed_tools=list(self.allowed_tools),
            disallowed_tools=list(DISALLOWED_TOOLS),
            permission_mode="dontAsk",
            mcp_servers={"mindflow": server},
            strict_mcp_config=True,
            setting_sources=[],
            settings=str(self.settings_path),
            plugins=[{"type": "local", "path": str(self.plugin_path)}],
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": SYSTEM_RULES,
                "exclude_dynamic_sections": True,
            },
            cwd=self.workdir,
            model=self.model,
            fallback_model=None,
            resume=resume_session_id,
            max_turns=self.max_turns,
            env=self._environment(),
            stderr=_safe_stderr,
        )
        return ProductionClaudeClient(sdk, options, expected_skill=SKILL_NAME)
