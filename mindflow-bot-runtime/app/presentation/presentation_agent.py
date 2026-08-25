"""Tool-less, stateless presentation-only Claude Agent SDK client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Protocol

from app.agent.sdk_adapter import DISALLOWED_TOOLS, _load_sdk, _safe_stderr


PRESENTATION_SYSTEM_RULES = """You are MindFlow's presentation compiler.

You do not make decisions and you do not add facts. Input is an already-reviewed
authoritative answer from another agent.

Your only task is to choose 1-3 delivery boundaries in the supplied authoritative
answer. You are a boundary selector, not a writer.

Hard rules:
- never output or rewrite answer text,
- spans must cover the complete answer exactly once, in its original order,
- the first start is 0, adjacent spans touch, and the final end equals the
  Python character length supplied in authoritative_answer_length,
- offsets use Python Unicode character indexes and end is exclusive,
- do not call tools,
- do not mention internal systems,
- output JSON only in the form
  {"spans":[{"start":0,"end":120},{"start":120,"end":240}]}.
"""


class PresentationAgentProtocol(Protocol):
    async def compose(
        self,
        text: str,
        *,
        response_kind: str,
        has_card: bool,
        max_segments: int,
    ) -> tuple[tuple[int, int], ...]: ...


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
        disconnect_timeout_seconds: float = 0.5,
    ):
        self.workdir = Path(workdir)
        self.model = str(model)
        self.base_url = str(base_url)
        self.auth_token = str(auth_token)
        self.opus_model = str(opus_model)
        self.sonnet_model = str(sonnet_model)
        self.haiku_model = str(haiku_model)
        self.disconnect_timeout_seconds = max(
            0.05, float(disconnect_timeout_seconds)
        )
        self._disconnect_cleanups: set[asyncio.Task[None]] = set()

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
    ) -> tuple[tuple[int, int], ...]:
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
                "authoritative_answer_length": len(str(text)),
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
            await self._disconnect_bounded(client)
        if result_message is None or result_message.is_error:
            raise ValueError("presentation agent returned no successful result")
        payload = json.loads(str(result_message.result or ""))
        spans = payload.get("spans") if isinstance(payload, dict) else None
        if not isinstance(spans, list):
            raise ValueError("presentation agent result has no spans")
        parsed = []
        for span in spans:
            if not isinstance(span, dict):
                raise ValueError("presentation span must be an object")
            start = span.get("start")
            end = span.get("end")
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
            ):
                raise ValueError("presentation span offsets must be integers")
            parsed.append((start, end))
        return tuple(parsed)

    async def _disconnect_bounded(self, client: object) -> None:
        """Do not let SDK process cleanup extend the user-visible deadline."""

        try:
            task = asyncio.create_task(
                client.disconnect(), name="presentation-agent-disconnect"
            )
        except Exception:
            return
        self._disconnect_cleanups.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._disconnect_cleanups.discard(done)
            if getattr(done, "cancelled", lambda: False)():
                return
            try:
                done.exception()
            except BaseException:
                pass

        task.add_done_callback(finished)
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=self.disconnect_timeout_seconds
            )
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task not in done:
            task.cancel()

    async def close(self) -> None:
        tasks = set(self._disconnect_cleanups)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self.disconnect_timeout_seconds)
