"""Thin production adapter around ClaudeSDKClient.

The Agent SDK owns the model/tool loop. This module only locks its production
configuration, translates streamed lifecycle messages, and normalizes errors.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping, Protocol

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

SYSTEM_RULES = """You are MindFlow, a natural, warm, non-clinical daily-care assistant in a private Feishu chat.

Conversation is the default. Reply directly to greetings, everyday conversation, emotional sharing, general explanations, and low-stakes suggestions. Do not call a tool merely because one exists. Acknowledge feelings without pretending to know facts the user did not state; do not interrogate, diagnose, screen, treat, or use clinical labels.

Use the mental-health-care skill only when the request needs participant-specific recorded data, a reviewed model result, a Feishu card, or a calendar action. Backend-provided identity is authoritative: never request, infer, repeat, pass, or change any participant/user/open/chat/calendar ID. Use only facts returned by MindFlow MCP tools. Keep self-reports, recorded observations, calendar facts, and model predictions clearly distinct.

Read-only tools may be used when they are necessary to answer the user's request. State-changing tools require a direct user request. Before creating or changing a calendar event, resolve any missing title/time/recurrence details. Before deleting, identify one exact event and obtain explicit confirmation; never infer consent from a suggestion. For recurring events, restate the frequency, interval, weekdays, and ending rule when confirmation is needed. Never claim success unless the tool returns ok=true.

When the user asks to fill in a state questionnaire or prefers buttons, send the reviewed check-in card. Card submissions are validated and stored by the backend, not by you; never invent a submitted result. Do not create arbitrary cards or arbitrary callback actions.

If a tool fails, explain the limitation briefly. Failure is not permission to use another channel. Never request secrets, tokens, SQL, file paths, shell commands, arbitrary URLs, or hidden identifiers. For possible immediate self-harm or suicide, do not run ordinary tools or calculate scores; the runtime supplies reviewed fixed support text.

Final responses must be concise, calm, optional, and suitable for a private Feishu chat."""

SYSTEM_RULES += """

The backend owns progress messages and final presentation formatting.
Do not narrate tool execution before calling a tool. Do not say that an action
succeeded until the tool result confirms ok=true.

For normal final replies, prefer plain natural text. Do not use Markdown
headings, bold markers, tables, or fenced blocks unless the user explicitly
requests code or a literal Markdown artifact. Do not manage message chunking;
the backend presentation layer owns segmentation and Feishu rendering."""

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

    async def run_turn(self, text: str) -> ClaudeTurnResult: ...

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

    async def run_turn(self, text: str) -> ClaudeTurnResult:
        self._interrupted = False
        result_message = None
        started_at = time.monotonic()
        first_text_delta_ms = None
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
                stream_event = getattr(self.sdk, "StreamEvent", None)
                if (
                    first_text_delta_ms is None
                    and stream_event is not None
                    and isinstance(message, stream_event)
                    and "text_delta" in str(getattr(message, "event", ""))
                ):
                    first_text_delta_ms = round(
                        (time.monotonic() - started_at) * 1000, 1
                    )
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
        if first_text_delta_ms is not None:
            logger.info(
                "claude_partial_telemetry",
                extra={"time_to_first_text_delta_ms": first_text_delta_ms},
            )
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
        partial_messages_enabled: bool = False,
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
        self.partial_messages_enabled = bool(partial_messages_enabled)

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
            include_partial_messages=self.partial_messages_enabled,
            env=self._environment(),
            stderr=_safe_stderr,
        )
        return ProductionClaudeClient(sdk, options, expected_skill=SKILL_NAME)
