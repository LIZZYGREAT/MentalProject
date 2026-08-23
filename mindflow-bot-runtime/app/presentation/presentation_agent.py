"""Tool-less, stateless presentation-only Claude Agent SDK client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from app.agent.sdk_adapter import DISALLOWED_TOOLS, _load_sdk, _safe_stderr


PRESENTATION_SYSTEM_RULES = """You are MindFlow's presentation compiler.

You do not make decisions and you do not add facts. Input is an already-reviewed
authoritative answer from another agent.

Your only tasks:
- remove redundancy,
- make the Chinese natural and concise,
- order information for readability,
- split long content into 1-3 user-facing message segments.

Hard rules:
- preserve every numeric value, date, time, status, and business outcome,
- do not introduce new facts,
- do not soften or reverse a success/failure result,
- do not use Markdown syntax,
- do not call tools,
- do not mention internal systems,
- output JSON only in the form {"segments":["first", "second"]}.
"""


class PresentationAgentProtocol(Protocol):
    async def compose(
        self,
        text: str,
        *,
        response_kind: str,
        has_card: bool,
        max_segments: int,
    ) -> tuple[str, ...]: ...


class ProductionPresentationAgent:
    """Creates a fresh no-tool SDK client for every presentation turn."""

    def __init__(
        self,
        *,
        workdir: Path,
        model: str,
        base_url: str,
        auth_token: str,
        opus_model: str,
        sonnet_model: str,
        haiku_model: str,
    ):
        self.workdir = Path(workdir)
        self.model = str(model)
        self.base_url = str(base_url)
        self.auth_token = str(auth_token)
        self.opus_model = str(opus_model)
        self.sonnet_model = str(sonnet_model)
        self.haiku_model = str(haiku_model)

    def _environment(self) -> dict[str, str]:
        return {
            "ANTHROPIC_AUTH_TOKEN": self.auth_token,
            "ANTHROPIC_BASE_URL": self.base_url,
            "ANTHROPIC_MODEL": self.model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": self.opus_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": self.sonnet_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.haiku_model,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_AGENT_SDK_CLIENT_APP": "mindflow-presentation/1.0",
        }

    async def compose(
        self,
        text: str,
        *,
        response_kind: str,
        has_card: bool,
        max_segments: int,
    ) -> tuple[str, ...]:
        sdk = _load_sdk()
        options = sdk.ClaudeAgentOptions(
            tools=[],
            skills=[],
            allowed_tools=[],
            disallowed_tools=list(DISALLOWED_TOOLS) + ["Skill"],
            permission_mode="dontAsk",
            setting_sources=[],
            plugins=[],
            system_prompt=PRESENTATION_SYSTEM_RULES,
            cwd=self.workdir,
            model=self.model,
            fallback_model=None,
            max_turns=1,
            env=self._environment(),
            stderr=_safe_stderr,
        )
        client = sdk.ClaudeSDKClient(options=options)
        result_message = None
        prompt = json.dumps(
            {
                "authoritative_answer": str(text),
                "response_kind": str(response_kind),
                "has_card": bool(has_card),
                "max_segments": int(max_segments),
            },
            ensure_ascii=False,
        )
        try:
            await client.connect()
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, sdk.ResultMessage):
                    result_message = message
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        if result_message is None or result_message.is_error:
            raise ValueError("presentation agent returned no successful result")
        payload = json.loads(str(result_message.result or ""))
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list):
            raise ValueError("presentation agent result has no segments")
        return tuple(str(item) for item in segments)

