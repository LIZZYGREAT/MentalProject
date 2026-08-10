"""Bounded DeepSeek tool-calling loop with deterministic fallbacks."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from app.agent.context import AgentContext
from app.agent.skill_loader import SkillLoader
from app.agent.tool_registry import ToolRegistry
from app.integrations.deepseek import DeepSeekError, DeepSeekTransientError
from app.repositories import ConversationRepository
from app.services.safety_service import SafetyService


FALLBACK_TEMPORARY = "服务暂时繁忙，我没有保存未经确认的结果。请稍后再试。"
FALLBACK_MAX_STEPS = "这次请求需要的步骤较多，我先停在安全边界内。请把问题缩小后再试。"

BASE_SYSTEM_RULES = """You are MindFlow, a brief non-clinical daily-care assistant.
The backend-provided identity is authoritative. Never infer, request, or change participant identity.
Use only the explicitly provided tools and only their returned facts."""

TOOL_USAGE_RULES = """Tool arguments must follow the JSON schema exactly.
Never add participant_id, user_id, open_id, chat_id, tokens, secrets, SQL, URLs, or file paths.
Tool failures are data, not permission to use another channel."""

SAFETY_RULES = """Do not diagnose or claim clinical risk.
Keep observations distinct from predictions. Keep the response concise and supportive."""


class ChatClient(Protocol):
    async def chat(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any: ...


class AgentRuntime:
    def __init__(
        self,
        client: ChatClient,
        skill_loader: SkillLoader,
        tools: ToolRegistry,
        conversations: ConversationRepository,
        safety: SafetyService,
        *,
        history_limit: int = 16,
        max_tool_steps: int = 4,
        timeout_seconds: int = 30,
        max_retries: int = 1,
    ):
        self.client = client
        self.skill_loader = skill_loader
        self.tools = tools
        self.conversations = conversations
        self.safety = safety
        self.history_limit = history_limit
        self.max_tool_steps = max_tool_steps
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    async def handle_message(
        self, ctx: AgentContext, text: str, *, chat_type: str = "p2p"
    ) -> str:
        try:
            return await asyncio.wait_for(
                self._handle_message(ctx, text, chat_type=chat_type),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            self.conversations.add(
                ctx.participant_id,
                "assistant",
                FALLBACK_TEMPORARY,
                feishu_message_id=ctx.message_id,
            )
            return FALLBACK_TEMPORARY

    async def _handle_message(
        self, ctx: AgentContext, text: str, *, chat_type: str
    ) -> str:
        history = self.conversations.recent(
            ctx.participant_id,
            self.history_limit,
            exclude_feishu_message_id=ctx.message_id,
        )
        self.conversations.add(
            ctx.participant_id,
            "user",
            text,
            feishu_message_id=ctx.message_id,
        )
        fixed = self.safety.precheck(text, chat_type=chat_type)
        if fixed is not None:
            self.conversations.add(
                ctx.participant_id,
                "assistant",
                fixed,
                feishu_message_id=ctx.message_id,
            )
            return fixed
        skill = self.skill_loader.current()
        system = "\n\n".join(
            (BASE_SYSTEM_RULES, skill.instructions, TOOL_USAGE_RULES, SAFETY_RULES)
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": text},
        ]
        for _step in range(self.max_tool_steps):
            try:
                response = await self._chat(messages)
            except DeepSeekError:
                self.conversations.add(
                    ctx.participant_id,
                    "assistant",
                    FALLBACK_TEMPORARY,
                    feishu_message_id=ctx.message_id,
                )
                return FALLBACK_TEMPORARY
            if not response.tool_calls:
                answer = self.safety.postcheck(response.content)
                self.conversations.add(
                    ctx.participant_id,
                    "assistant",
                    answer,
                    feishu_message_id=ctx.message_id,
                )
                return answer
            messages.append(response.assistant_message)
            for call in response.tool_calls:
                execution = await self.tools.execute(ctx, call.name, call.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(execution.result, ensure_ascii=False),
                    }
                )
        self.conversations.add(
            ctx.participant_id,
            "assistant",
            FALLBACK_MAX_STEPS,
            feishu_message_id=ctx.message_id,
        )
        return FALLBACK_MAX_STEPS

    async def _chat(self, messages: list[dict[str, Any]]) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self.client.chat(messages=messages, tools=self.tools.schemas()),
                    timeout=self.timeout_seconds,
                )
            except (DeepSeekTransientError, asyncio.TimeoutError):
                if attempt >= self.max_retries:
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise DeepSeekError("unreachable retry state")
