"""Durable Feishu orchestration around participant-scoped Agent SDK queues."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

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
from app.repositories import (
    AgentRunRepository,
    BotEventRepository,
    RuntimeIncidentRepository,
)
from app.presentation.contracts import (
    AgentActivityCallback,
    AgentActivityEvent,
    ResponsePlan,
    ResponseSegment,
    RuntimeResponse,
)
from app.presentation.progress_policy import should_force_silent_progress
from app.presentation.progress_presenter import ProgressPresenter
from app.presentation.response_orchestrator import ResponseOrchestrator
from app.services.presentation_service import PresentationOutbox
from app.services.presentation_service import PendingImageCard


logger = logging.getLogger(__name__)
BIND_PATTERN = re.compile(r"^/bind(?:\s+(\S+))?\s*$", re.IGNORECASE)
CALENDAR_CONNECT_PATTERN = re.compile(
    r"^/(?:calendar|connect-calendar)\s*$", re.IGNORECASE
)
STOP_PATTERN = re.compile(r"^/stop\s*$", re.IGNORECASE)

class AgentRuntimeProtocol(Protocol):
    async def handle_message(
        self,
        ctx: AgentContext,
        text: str,
        *,
        chat_type: str = "p2p",
        on_activity: AgentActivityCallback | None = None,
    ) -> RuntimeResponse: ...

    async def interrupt(self, participant_id) -> bool: ...


@dataclass
class ProgressState:
    sent: int = 0
    last_sent_at: float = 0.0
    used_tools: set[str] = field(default_factory=set)
    sent_keys: set[str] = field(default_factory=set)
    last_stage: str | None = None
    first_activity_at: float | None = None
    first_tool_started_at: float | None = None
    tool_started_at: dict[str, float] = field(default_factory=dict)
    tool_durations_ms: list[float] = field(default_factory=list)
    force_silent: bool = False
    tool_activity_seen: bool = False
    final_ready: bool = False
    pending_text: str | None = None
    pending_key: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _log(status: str, **fields: object) -> None:
    safe: dict[str, object] = {
        "event": fields.get("event", "response_delivery_completed"),
        "status": status,
        "participant_id": fields.get("participant_id"),
        "message_id": fields.get("message_id"),
        "event_id": fields.get("event_id"),
        "agent_run_id": fields.get("agent_run_id"),
        "tool_name": fields.get("tool_name"),
        "latency_ms": fields.get("latency_ms"),
    }
    for name in (
        "received_to_agent_start_ms",
        "agent_start_to_first_activity_ms",
        "first_tool_start_ms",
        "tool_duration_ms",
        "agent_result_ms",
        "presentation_ms",
        "card_upload_ms",
        "first_final_send_ms",
        "total_delivery_ms",
        "segment_count",
        "presentation_agent_used",
        "presentation_agent_attempted",
        "presentation_agent_outcome",
        "presentation_agent_latency_ms",
        "presentation_cleanup_pending",
    ):
        if fields.get(name) is not None:
            safe[name] = fields[name]
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
        presentations: PresentationOutbox | None = None,
        *,
        model: str,
        progress_presenter: ProgressPresenter | None = None,
        response_orchestrator: ResponseOrchestrator | None = None,
        max_retries: int = 1,
        generic_progress_delay_seconds: float = 10.0,
        tool_progress_grace_seconds: float = 1.2,
        progress_cooldown_seconds: int = 3,
        progress_max_messages: int = 2,
        incidents: RuntimeIncidentRepository | None = None,
    ):
        self.queue = queue
        self.identity = identity
        self.events = events
        self.runs = runs
        self.skill_loader = skill_loader
        self.runtime = runtime
        self.sender = sender
        self.device_flows = device_flows
        self.presentations = presentations
        self.progress_presenter = progress_presenter or ProgressPresenter()
        self.response_orchestrator = response_orchestrator or ResponseOrchestrator()
        self.model = model
        self.max_retries = max_retries
        self.generic_progress_delay_seconds = max(
            0.0, float(generic_progress_delay_seconds)
        )
        self.tool_progress_grace_seconds = max(
            0.0, float(tool_progress_grace_seconds)
        )
        self.progress_cooldown_seconds = progress_cooldown_seconds
        # A bot event owns at most one user-visible processing message. Keep
        # accepting the legacy setting so existing deployments do not fail at
        # startup, but never allow it to weaken the ordering invariant.
        self.progress_max_messages = min(1, max(0, int(progress_max_messages)))
        self.incidents = incidents
        self._routing_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._active_event_by_participant: dict[object, str] = {}
        self._cancelled_participants: set[object] = set()

    async def _record_incident(self, **values) -> None:
        if self.incidents is None:
            return
        try:
            await asyncio.to_thread(self.incidents.record, **values)
        except Exception:
            logger.warning("runtime_incident_persist_failed", exc_info=True)

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
                self._process_queued(event), name=f"feishu-event-{event.event_id}"
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
            await asyncio.to_thread(
                self.events.finish,
                event.event_id,
                status="failed",
                error_code="unhandled_worker_failure",
            )
            await self._record_incident(
                severity="error",
                subsystem="worker",
                event_name="worker_event_unhandled",
                bot_event_id=event.event_id,
                error_code="unhandled_worker_failure",
                summary="Bot worker could not handle a queued event.",
            )
        finally:
            self.queue.task_done()

    async def process(self, event: BotEvent) -> None:
        route_key = f"{event.app_id}:{event.open_id}"
        lock = self._routing_locks.setdefault(route_key, asyncio.Lock())
        agent_task: asyncio.Task[None] | None = None
        async with lock:
            participant = await asyncio.to_thread(
                self.identity.resolve, event.app_id, event.open_id
            )
            pending_plan = await asyncio.to_thread(
                self.events.pending_reply_plan, event.event_id
            )
            if pending_plan is not None:
                await self._resume_delivery_plan(
                    event,
                    pending_plan,
                    participant_id=(participant.id if participant is not None else None),
                )
                return
            await asyncio.to_thread(
                self.events.set_processing,
                event.event_id,
                participant.id if participant is not None else None,
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
                    participant = await asyncio.to_thread(
                        self.identity.bind,
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
                await asyncio.to_thread(
                    self.events.assign_participant, event.event_id, participant.id
                )
                await self._deliver(event, f"绑定成功：{participant.participant_code}")
                return
            if bind_match is not None:
                await self._deliver(event, "当前飞书账号已经绑定。")
                return
            if STOP_PATTERN.match(event.text):
                self._cancelled_participants.add(participant.id)
                active_event_id = self._active_event_by_participant.get(participant.id)
                if active_event_id:
                    await asyncio.to_thread(
                        self.events.cancel_reply_plan, active_event_id
                    )
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
            self._cancelled_participants.discard(participant.id)
            run_id = await asyncio.to_thread(
                self.runs.start,
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
            self._active_event_by_participant[participant.id] = event.event_id
        if agent_task is not None:
            await agent_task

    async def _run_agent(self, event: BotEvent, ctx: AgentContext, run_id) -> None:
        started = time.monotonic()
        progress = ProgressState(
            force_silent=should_force_silent_progress(event.text)
        )
        message_created_at = event.create_time
        if message_created_at.tzinfo is None:
            message_created_at = message_created_at.replace(tzinfo=timezone.utc)
        metrics: dict[str, object] = {
            "received_to_agent_start_ms": max(
                0.0,
                round(
                    (datetime.now(timezone.utc) - message_created_at).total_seconds()
                    * 1000,
                    1,
                ),
            )
        }

        async def emit_locked(text: str, *, key: str) -> None:
            """Send while holding progress.lock so final cannot overtake it."""

            now = time.monotonic()
            if progress.final_ready:
                return
            if progress.sent >= self.progress_max_messages:
                return
            if key in progress.sent_keys:
                return
            if (
                progress.sent
                and now - progress.last_sent_at < self.progress_cooldown_seconds
            ):
                return
            try:
                await self._send(
                    event.chat_id,
                    text,
                    message_uuid=self._stable_message_uuid(
                        f"mindflow:progress:{event.event_id}"
                    ),
                )
            except FeishuSendError:
                return
            progress.sent += 1
            progress.last_sent_at = now
            progress.sent_keys.add(key)

        async def delayed_generic_progress() -> None:
            await asyncio.sleep(self.generic_progress_delay_seconds)
            async with progress.lock:
                if (
                    progress.final_ready
                    or progress.force_silent
                    or progress.sent
                    or progress.tool_activity_seen
                ):
                    return
                suggestion = self.progress_presenter.delayed(
                    event.text, state=progress
                )
                if suggestion:
                    await emit_locked(suggestion, key="delayed")

        async def delayed_tool_progress() -> None:
            await asyncio.sleep(self.tool_progress_grace_seconds)
            async with progress.lock:
                if progress.final_ready:
                    return
                suggestion = progress.pending_text
                key = progress.pending_key
                if suggestion and key:
                    await emit_locked(suggestion, key=key)

        tool_timer: asyncio.Task[None] | None = None

        async def on_activity(activity: AgentActivityEvent) -> None:
            nonlocal tool_timer
            async with progress.lock:
                now = time.monotonic()
                if progress.first_activity_at is None:
                    progress.first_activity_at = now
                tool_name = str(activity.tool_name or "")
                if tool_name:
                    progress.used_tools.add(tool_name)
                if activity.kind == "tool_started" and tool_name:
                    progress.tool_activity_seen = True
                    if progress.first_tool_started_at is None:
                        progress.first_tool_started_at = now
                    progress.tool_started_at[tool_name] = now
                elif activity.kind in {"tool_succeeded", "tool_failed"} and tool_name:
                    tool_started = progress.tool_started_at.pop(tool_name, None)
                    if tool_started is not None:
                        progress.tool_durations_ms.append(
                            round((now - tool_started) * 1000, 1)
                        )
                suggestion = self.progress_presenter.present(activity, state=progress)
                if suggestion:
                    key = self.progress_presenter.key_for(activity, state=progress)
                    progress.pending_text = suggestion
                    progress.pending_key = key
                    if tool_timer is None or tool_timer.done():
                        tool_timer = asyncio.create_task(
                            delayed_tool_progress(),
                            name=f"tool-progress-{event.event_id}",
                        )

        async def close_progress_before_final() -> None:
            # If a processing send already owns the lock, wait until the
            # provider call has completed. Otherwise mark final ready first so
            # a threshold-edge timer can no longer start a processing send.
            async with progress.lock:
                progress.final_ready = True
            generic_timer.cancel()
            timers = [generic_timer]
            if tool_timer is not None:
                tool_timer.cancel()
                timers.append(tool_timer)
            await asyncio.gather(*timers, return_exceptions=True)

        generic_timer = asyncio.create_task(
            delayed_generic_progress(),
            name=f"generic-progress-{event.event_id}",
        )
        try:
            agent_started = time.monotonic()
            response = await self.runtime.handle_message(
                ctx,
                event.text,
                chat_type=event.chat_type,
                on_activity=on_activity,
            )
            metrics["agent_result_ms"] = round(
                (time.monotonic() - agent_started) * 1000, 1
            )
            await close_progress_before_final()
            if ctx.participant_id in self._cancelled_participants:
                raise ClaudeRuntimeInterrupted(FALLBACK_INTERRUPTED)
            cards = (
                self.presentations.take_cards(run_id)
                if self.presentations is not None
                else []
            )
            card_delivery_failed = False
            delivered_cards: list[object] = []
            card_started = time.monotonic()
            for card in cards:
                try:
                    if isinstance(card, PendingImageCard):
                        await self._send_image_card(event.chat_id, card)
                    else:
                        await self._send_card(event.chat_id, card)
                    delivered_cards.append(card)
                except FeishuSendError as exc:
                    card_delivery_failed = True
                    logger.warning(
                        "feishu_card_send_failed event_id=%s message_id=%s "
                        "error_code=%s retryable=%s",
                        event.event_id,
                        event.message_id,
                        exc.code,
                        exc.retryable,
                    )
                    await self._record_incident(
                        severity="error",
                        subsystem="feishu",
                        event_name="feishu_card_send_failed",
                        participant_id=ctx.participant_id,
                        bot_event_id=event.event_id,
                        error_code=str(exc.code) if exc.code is not None else None,
                        error_class=type(exc).__name__,
                        summary="A reviewed Feishu card could not be delivered.",
                    )
            metrics["card_upload_ms"] = round(
                (time.monotonic() - card_started) * 1000, 1
            )
            if card_delivery_failed:
                authoritative = (
                    response
                    if isinstance(response, RuntimeResponse)
                    else RuntimeResponse(text=str(response))
                )
                response = RuntimeResponse(
                    text=authoritative.text + "\n\n卡片暂时未能发送，请稍后再试。",
                    safety_locked=authoritative.safety_locked,
                    response_kind=authoritative.response_kind,
                )
            presentation_started = time.monotonic()
            plan = await self.response_orchestrator.build_plan(
                response,
                cards=delivered_cards,
                used_tools=progress.used_tools,
            )
            metrics["presentation_ms"] = round(
                (time.monotonic() - presentation_started) * 1000, 1
            )
            metrics["segment_count"] = len(plan.segments)
            metrics["presentation_agent_used"] = plan.presentation_agent_used
            metrics["presentation_agent_attempted"] = (
                plan.presentation_agent_attempted
            )
            metrics["presentation_agent_outcome"] = (
                plan.presentation_agent_outcome
            )
            metrics["presentation_agent_latency_ms"] = (
                plan.presentation_agent_latency_ms
            )
            metrics["presentation_cleanup_pending"] = (
                plan.presentation_cleanup_pending
            )
            await asyncio.to_thread(self.runs.finish, run_id, "succeeded")
            delivered = await self._deliver_plan(
                event,
                plan,
                participant_id=ctx.participant_id,
                metrics=metrics,
                delivery_started_at=started,
            )
            status = "completed" if delivered else "reply_pending"
        except ClaudeRuntimeInterrupted:
            await close_progress_before_final()
            if self.presentations is not None:
                self.presentations.discard(run_id)
            await asyncio.to_thread(self.runs.finish, run_id, "interrupted")
            delivered = await self._deliver(event, FALLBACK_INTERRUPTED)
            status = "interrupted" if delivered else "reply_pending"
        except Exception:
            await close_progress_before_final()
            if self.presentations is not None:
                self.presentations.discard(run_id)
            logger.exception(
                "bot_event_failed",
                extra={
                    "participant_id": str(ctx.participant_id),
                    "event_id": event.event_id,
                    "message_id": event.message_id,
                    "agent_run_id": str(run_id),
                },
            )
            await asyncio.to_thread(self.runs.finish, run_id, "failed")
            delivered = await self._deliver(event, FALLBACK_TEMPORARY)
            status = "failed_replied" if delivered else "reply_pending"
        finally:
            generic_timer.cancel()
            timers = [generic_timer]
            if tool_timer is not None:
                tool_timer.cancel()
                timers.append(tool_timer)
            await asyncio.gather(*timers, return_exceptions=True)
            if self._active_event_by_participant.get(ctx.participant_id) == event.event_id:
                self._active_event_by_participant.pop(ctx.participant_id, None)
        if progress.first_activity_at is not None:
            metrics["agent_start_to_first_activity_ms"] = round(
                (progress.first_activity_at - started) * 1000, 1
            )
        if progress.first_tool_started_at is not None:
            metrics["first_tool_start_ms"] = round(
                (progress.first_tool_started_at - started) * 1000, 1
            )
        if progress.tool_durations_ms:
            metrics["tool_duration_ms"] = progress.tool_durations_ms
        metrics["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        save_telemetry = getattr(self.events, "save_telemetry", None)
        if callable(save_telemetry):
            try:
                await asyncio.to_thread(save_telemetry, event.event_id, metrics)
            except Exception:
                logger.warning(
                    "bot_event_telemetry_persist_failed event_id=%s",
                    event.event_id,
                    exc_info=True,
                )
        _log(
            status,
            participant_id=str(ctx.participant_id),
            message_id=event.message_id,
            event_id=event.event_id,
            agent_run_id=str(run_id),
            **metrics,
        )

    async def _deliver(self, event: BotEvent, text: str) -> bool:
        plan = ResponsePlan(
            kind="fixed",
            full_text=str(text),
            segments=(ResponseSegment(0, str(text)),),
            use_cards=False,
        )
        return await self._deliver_plan(event, plan)

    async def _deliver_plan(
        self,
        event: BotEvent,
        plan: ResponsePlan,
        *,
        participant_id=None,
        metrics: dict[str, object] | None = None,
        delivery_started_at: float | None = None,
    ) -> bool:
        if not plan.segments:
            await asyncio.to_thread(
                self.events.finish, event.event_id, status="completed"
            )
            return True
        await asyncio.to_thread(
            self.events.stage_reply_plan,
            event.event_id,
            full_text=plan.full_text,
            segments=[segment.text for segment in plan.segments],
        )
        pending = await asyncio.to_thread(
            self.events.pending_reply_plan, event.event_id
        )
        if pending is None:
            return False
        return await self._resume_delivery_plan(
            event,
            pending,
            participant_id=participant_id,
            metrics=metrics,
            delivery_started_at=delivery_started_at,
        )

    async def _resume_delivery_plan(
        self,
        event: BotEvent,
        pending_plan,
        *,
        participant_id=None,
        metrics: dict[str, object] | None = None,
        delivery_started_at: float | None = None,
    ) -> bool:
        delivery_started = time.monotonic()
        first_final_recorded = False
        for index in range(pending_plan.next_segment, len(pending_plan.segments)):
            if (
                participant_id is not None
                and participant_id in self._cancelled_participants
            ):
                await asyncio.to_thread(
                    self.events.cancel_reply_plan, event.event_id
                )
                return False
            try:
                message_id = await self._send(
                    event.chat_id,
                    pending_plan.segments[index],
                    message_uuid=self._stable_message_uuid(
                        f"mindflow:reply:{event.event_id}:{index}"
                    ),
                )
            except FeishuSendError as exc:
                await asyncio.to_thread(
                    self.events.note_reply_failure, event.event_id
                )
                logger.warning(
                    "feishu_reply_send_failed event_id=%s message_id=%s "
                    "segment_index=%s error_code=%s retryable=%s attempt=%s",
                    event.event_id,
                    event.message_id,
                    index,
                    exc.code,
                    exc.retryable,
                    getattr(exc, "attempt", 1),
                )
                await self._record_incident(
                    severity="error",
                    subsystem="feishu",
                    event_name="feishu_reply_send_failed",
                    participant_id=participant_id,
                    bot_event_id=event.event_id,
                    error_code=str(exc.code) if exc.code is not None else None,
                    error_class=type(exc).__name__,
                    summary="A durable reply segment could not be delivered.",
                    details={"segment_index": index},
                )
                return False
            if metrics is not None and not first_final_recorded:
                metrics["first_final_send_ms"] = round(
                    (
                        time.monotonic()
                        - (delivery_started_at or delivery_started)
                    )
                    * 1000,
                    1,
                )
                first_final_recorded = True
            await asyncio.to_thread(
                self.events.mark_reply_segment_sent,
                event.event_id,
                segment_index=index,
                message_id=message_id,
            )
        await asyncio.to_thread(self.events.finish_reply_plan, event.event_id)
        if metrics is not None:
            metrics["total_delivery_ms"] = round(
                (time.monotonic() - delivery_started) * 1000, 1
            )
        return True

    async def _send(
        self,
        chat_id: str,
        text: str,
        *,
        message_uuid: str | None = None,
    ) -> str:
        for attempt in range(self.max_retries + 1):
            try:
                if message_uuid and self._supports_message_uuid():
                    return await asyncio.to_thread(
                        self.sender.send_text,
                        chat_id,
                        text,
                        message_uuid=message_uuid,
                    )
                return await asyncio.to_thread(self.sender.send_text, chat_id, text)
            except FeishuSendError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    exc.attempt = attempt + 1
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise FeishuSendError(FALLBACK_TEMPORARY)

    def _supports_message_uuid(self) -> bool:
        import inspect

        try:
            parameters = inspect.signature(self.sender.send_text).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.name == "message_uuid"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    @staticmethod
    def _stable_message_uuid(key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(key)))

    async def _send_card(self, chat_id: str, card: dict) -> str:
        send_card = getattr(self.sender, "send_card", None)
        if not callable(send_card):
            raise FeishuSendError("Feishu card sending is unavailable", retryable=False)
        for attempt in range(self.max_retries + 1):
            try:
                return await asyncio.to_thread(send_card, chat_id, card)
            except FeishuSendError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    exc.attempt = attempt + 1
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise FeishuSendError(FALLBACK_TEMPORARY)

    async def _send_image_card(
        self, chat_id: str, presentation: PendingImageCard
    ) -> str:
        upload_image = getattr(self.sender, "upload_image", None)
        if not callable(upload_image):
            raise FeishuSendError("Feishu image upload is unavailable", retryable=False)
        image_key = ""
        for attempt in range(self.max_retries + 1):
            try:
                image_key = await asyncio.to_thread(
                    upload_image, presentation.png_bytes
                )
                break
            except FeishuSendError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    exc.attempt = attempt + 1
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        if not image_key:
            raise FeishuSendError("Feishu image upload returned no image_key")
        return await self._send_card(
            chat_id, presentation.materialize(str(image_key))
        )

    async def close(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close_orchestrator = getattr(self.response_orchestrator, "close", None)
        if callable(close_orchestrator):
            await close_orchestrator()
