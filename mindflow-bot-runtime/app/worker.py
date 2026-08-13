"""Durable Feishu orchestration around participant-scoped Agent SDK queues."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from app.agent.claude_runtime import (
    FALLBACK_INTERRUPTED,
    FALLBACK_TEMPORARY,
    ClaudeRuntimeInterrupted,
)
from app.agent.context import AgentContext
from app.agent.skill_loader import SkillLoader
from app.identity.service import BindingError, IdentityService
from app.integrations.feishu.client import FeishuClient, FeishuSendError
from app.integrations.feishu.gateway import BotEvent
from app.integrations.feishu.oauth import DeviceFlowService
from app.repositories import AgentRunRepository, BotEventRepository


logger = logging.getLogger(__name__)
BIND_PATTERN = re.compile(r"^/bind(?:\s+(\S+))?\s*$", re.IGNORECASE)
CALENDAR_CONNECT_PATTERN = re.compile(
    r"^/(?:calendar|connect-calendar)\s*$", re.IGNORECASE
)
STOP_PATTERN = re.compile(r"^/stop\s*$", re.IGNORECASE)

TOOL_PROGRESS_TEXT = {
    "care_get_today_context": "正在读取已记录的状态……",
    "care_get_recent_state": "正在读取最近的状态记录……",
    "care_run_today_assessment": "正在读取今天的数据并进行评估……",
    "calendar_connection_status": "正在检查日历连接状态……",
}

ProgressCallback = Callable[[str], Awaitable[None]]


class AgentRuntimeProtocol(Protocol):
    async def handle_message(
        self,
        ctx: AgentContext,
        text: str,
        *,
        chat_type: str = "p2p",
        on_tool_use: ProgressCallback | None = None,
    ) -> str: ...

    async def interrupt(self, participant_id) -> bool: ...


@dataclass
class ProgressState:
    sent: int = 0
    last_sent_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _log(status: str, **fields: object) -> None:
    safe = {
        "status": status,
        "participant_id": fields.get("participant_id"),
        "message_id": fields.get("message_id"),
        "event_id": fields.get("event_id"),
        "agent_run_id": fields.get("agent_run_id"),
        "tool_name": fields.get("tool_name"),
        "latency_ms": fields.get("latency_ms"),
    }
    logger.info(json.dumps(safe, ensure_ascii=False))


class BotWorker:
    def __init__(
        self,
        queue: asyncio.Queue[BotEvent],
        identity: IdentityService,
        events: BotEventRepository,
        runs: AgentRunRepository,
        skill_loader: SkillLoader,
        runtime: AgentRuntimeProtocol,
        sender: FeishuClient,
        device_flows: DeviceFlowService | None = None,
        *,
        model: str,
        max_retries: int = 1,
        progress_delay_seconds: int = 6,
        progress_cooldown_seconds: int = 8,
        progress_max_messages: int = 2,
    ):
        self.queue = queue
        self.identity = identity
        self.events = events
        self.runs = runs
        self.skill_loader = skill_loader
        self.runtime = runtime
        self.sender = sender
        self.device_flows = device_flows
        self.model = model
        self.max_retries = max_retries
        self.progress_delay_seconds = progress_delay_seconds
        self.progress_cooldown_seconds = progress_cooldown_seconds
        self.progress_max_messages = progress_max_messages
        self._routing_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    def resume_device_flow(self, participant_id) -> None:
        if self.device_flows is None:
            return
        task = asyncio.create_task(
            self.device_flows.poll_until_complete(participant_id),
            name=f"calendar-device-flow-{participant_id}",
        )
        self._track(task)

    def _track(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._background_tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logger.error(
                    "bot_background_task_failed",
                    extra={"error_type": type(error).__name__},
                )

        task.add_done_callback(finished)

    async def run_forever(self) -> None:
        """Keep ingress responsive while SessionManager bounds Agent concurrency."""

        while True:
            event = await self.queue.get()
            task = asyncio.create_task(
                self._process_queued(event), name=f"bot-event-{event.event_id}"
            )
            self._track(task)

    async def _process_queued(self, event: BotEvent) -> None:
        try:
            await self.process(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "worker_event_unhandled",
                extra={"event_id": event.event_id, "message_id": event.message_id},
            )
            self.events.finish(
                event.event_id,
                status="failed",
                error_code="unhandled_worker_failure",
            )
        finally:
            self.queue.task_done()

    async def process(self, event: BotEvent) -> None:
        route_key = f"{event.app_id}:{event.open_id}"
        lock = self._routing_locks.setdefault(route_key, asyncio.Lock())
        agent_task: asyncio.Task[None] | None = None
        async with lock:
            participant = self.identity.resolve(event.app_id, event.open_id)
            pending_reply = self.events.pending_reply(event.event_id)
            if pending_reply is not None:
                await self._deliver(event, pending_reply)
                return
            self.events.set_processing(
                event.event_id, participant.id if participant is not None else None
            )
            if event.chat_type.lower() not in {"p2p", "private", "single"}:
                await self._deliver(
                    event, "为了保护隐私，请在机器人单聊中使用 MindFlow。"
                )
                return
            bind_match = BIND_PATTERN.match(event.text)
            if participant is None:
                if bind_match is None:
                    await self._deliver(event, "尚未绑定。请发送：/bind 你的绑定码")
                    return
                raw_token = bind_match.group(1)
                if not raw_token:
                    await self._deliver(event, "请在 /bind 后填写一次性绑定码。")
                    return
                try:
                    participant = self.identity.bind(
                        raw_token=raw_token,
                        app_id=event.app_id,
                        open_id=event.open_id,
                        chat_id=event.chat_id,
                    )
                except BindingError:
                    await self._deliver(event, "绑定码无效、已使用或已过期。")
                    return
                except Exception:
                    await self._deliver(event, "绑定服务暂时不可用，请稍后重试。")
                    return
                self.events.assign_participant(event.event_id, participant.id)
                await self._deliver(event, f"绑定成功：{participant.participant_code}")
                return
            if bind_match is not None:
                await self._deliver(event, "当前飞书账号已经绑定。")
                return
            if STOP_PATTERN.match(event.text):
                interrupt = getattr(self.runtime, "interrupt", None)
                stopped = await interrupt(participant.id) if interrupt else False
                await self._deliver(
                    event,
                    "已请求停止当前处理。" if stopped else "当前没有正在处理的任务。",
                )
                return
            if CALENDAR_CONNECT_PATTERN.match(event.text):
                if self.device_flows is None:
                    await self._deliver(event, "日历授权暂时不可用。")
                    return
                try:
                    details = await self.device_flows.start(participant.id)
                except Exception:
                    await self._deliver(
                        event, "日历授权暂时无法启动，请稍后重试。"
                    )
                    return
                await self._deliver(
                    event,
                    "请打开以下飞书授权地址并输入验证码：\n"
                    f"{details['verification_url']}\n"
                    f"验证码：{details['user_code']}",
                )
                self.resume_device_flow(participant.id)
                return
            if participant.external_llm_consent_at is None:
                await self._deliver(
                    event,
                    "尚未记录将本次对话发送给外部模型的实验授权，请先联系研究者。",
                )
                return

            skill = self.skill_loader.current()
            run_id = self.runs.start(
                participant.id,
                event.message_id,
                self.model,
                skill.version,
            )
            ctx = AgentContext(
                participant_id=participant.id,
                participant_code=participant.participant_code,
                open_id=event.open_id,
                chat_id=event.chat_id,
                message_id=event.message_id,
                agent_run_id=run_id,
            )
            # Creating the task under the routing lock preserves arrival order;
            # the lock is released before the long Agent turn so /stop can pass.
            agent_task = asyncio.create_task(
                self._run_agent(event, ctx, run_id),
                name=f"agent-turn-{event.event_id}",
            )
        if agent_task is not None:
            await agent_task

    async def _run_agent(self, event: BotEvent, ctx: AgentContext, run_id) -> None:
        started = time.monotonic()
        progress = ProgressState()

        async def emit(text: str) -> None:
            async with progress.lock:
                now = time.monotonic()
                if progress.sent >= self.progress_max_messages:
                    return
                if (
                    progress.sent
                    and now - progress.last_sent_at < self.progress_cooldown_seconds
                ):
                    return
                try:
                    await self._send(event.chat_id, text)
                except FeishuSendError:
                    return
                progress.sent += 1
                progress.last_sent_at = now

        async def delayed_progress() -> None:
            await asyncio.sleep(self.progress_delay_seconds)
            await emit("正在处理，请稍候……")

        async def on_tool_use(tool_name: str) -> None:
            text = TOOL_PROGRESS_TEXT.get(tool_name)
            if text:
                await emit(text)

        timer = asyncio.create_task(delayed_progress())
        try:
            answer = await self.runtime.handle_message(
                ctx,
                event.text,
                chat_type=event.chat_type,
                on_tool_use=on_tool_use,
            )
            self.runs.finish(run_id, "succeeded")
            delivered = await self._deliver(event, answer)
            status = "completed" if delivered else "reply_pending"
        except ClaudeRuntimeInterrupted:
            self.runs.finish(run_id, "interrupted")
            delivered = await self._deliver(event, FALLBACK_INTERRUPTED)
            status = "interrupted" if delivered else "reply_pending"
        except Exception:
            logger.exception(
                "bot_event_failed",
                extra={
                    "participant_id": str(ctx.participant_id),
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "agent_run_id": str(run_id),
                },
            )
            self.runs.finish(run_id, "failed")
            delivered = await self._deliver(event, FALLBACK_TEMPORARY)
            status = "failed_replied" if delivered else "reply_pending"
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        _log(
            status,
            participant_id=str(ctx.participant_id),
            message_id=event.message_id,
            event_id=event.event_id,
            agent_run_id=str(run_id),
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )

    async def _deliver(self, event: BotEvent, text: str) -> bool:
        """Persist the final reply before sending so restart can finish delivery."""

        self.events.stage_reply(event.event_id, text)
        try:
            message_id = await self._send(event.chat_id, text)
        except FeishuSendError as exc:
            self.events.note_reply_failure(event.event_id)
            logger.warning(
                "feishu_reply_send_failed event_id=%s message_id=%s "
                "error_code=%s retryable=%s attempt=%s",
                event.event_id,
                event.message_id,
                exc.code,
                exc.retryable,
                getattr(exc, "attempt", 1),
            )
            return False
        self.events.finish(
            event.event_id,
            status="completed",
            reply_message_id=message_id,
        )
        return True

    async def _send(self, chat_id: str, text: str) -> str:
        for attempt in range(self.max_retries + 1):
            try:
                return await asyncio.to_thread(self.sender.send_text, chat_id, text)
            except FeishuSendError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    exc.attempt = attempt + 1
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise FeishuSendError(FALLBACK_TEMPORARY)

    async def close(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
