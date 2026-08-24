"""Safety and audit wrapper around the Claude Agent SDK session manager."""

from __future__ import annotations

from app.agent.context import AgentContext
from app.agent.sdk_adapter import (
    ClaudeSDKInvocationError,
    ClaudeSDKTurnInterrupted,
)
from app.agent.session_manager import ParticipantSessionManager
from app.presentation.contracts import (
    AgentActivityCallback,
    RuntimeResponse,
)
from app.repositories import ConversationRepository
from app.services.safety_service import SafetyService


FALLBACK_TEMPORARY = "服务暂时繁忙，我没有保存未经确认的结果。请稍后再试。"
FALLBACK_INTERRUPTED = "当前处理已停止。"


class ClaudeRuntimeInterrupted(RuntimeError):
    pass


class ClaudeAgentRuntime:
    def __init__(
        self,
        sessions: ParticipantSessionManager,
        conversations: ConversationRepository,
        safety: SafetyService,
    ):
        self.sessions = sessions
        self.conversations = conversations
        self.safety = safety

    async def handle_message(
        self,
        ctx: AgentContext,
        text: str,
        *,
        chat_type: str = "p2p",
        on_activity: AgentActivityCallback | None = None,
    ) -> RuntimeResponse:
        self.conversations.add(
            ctx.participant_id,
            "user",
            text,
            feishu_message_id=ctx.message_id,
        )
        fixed = self.safety.precheck(text, chat_type=chat_type)
        if fixed is not None:
            self._save_answer(ctx, fixed)
            return RuntimeResponse(
                text=fixed,
                safety_locked=True,
                response_kind="fixed",
            )
        try:
            result = await self.sessions.submit(
                ctx, text, on_activity=on_activity
            )
        except ClaudeSDKTurnInterrupted as exc:
            self._save_answer(ctx, FALLBACK_INTERRUPTED)
            raise ClaudeRuntimeInterrupted(FALLBACK_INTERRUPTED) from exc
        except ClaudeSDKInvocationError:
            self._save_answer(ctx, FALLBACK_TEMPORARY)
            raise
        answer = self.safety.postcheck(result.text)
        self._save_answer(ctx, answer)
        return RuntimeResponse(
            text=answer,
            safety_locked=False,
            response_kind="conversation",
        )

    async def interrupt(self, participant_id) -> bool:
        return await self.sessions.interrupt(participant_id)

    async def close(self) -> None:
        await self.sessions.close()

    def _save_answer(self, ctx: AgentContext, text: str) -> None:
        self.conversations.add(
            ctx.participant_id,
            "assistant",
            text,
            feishu_message_id=ctx.message_id,
        )
