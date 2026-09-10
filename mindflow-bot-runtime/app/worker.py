"""Durable Feishu orchestration around participant-scoped Agent SDK queues."""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal, Protocol

from app.agent.claude_runtime import (
    FALLBACK_INTERRUPTED,
    FALLBACK_TEMPORARY,
    ClaudeRuntimeInterrupted,
)
from app.agent.context import (
    AgentContext,
    CalendarMutationPolicy,
    SourceKind,
    TurnEffectPolicy,
)
from app.contracts.agent_input import AgentTurnInput
from app.contracts.generic_image_context import GenericImageContext
from app.agent.skill_loader import SkillLoader
from app.identity.service import BindingError, IdentityService
from app.integrations.feishu.client import FeishuClient, FeishuSendError
from app.integrations.feishu.gateway import BotEvent
from app.integrations.feishu.cards import course_schedule_preview_card
from app.integrations.feishu.message_resources import (
    MessageResourceError,
    MessageResourceTooLarge,
    UnsupportedImageFormat,
)
from app.integrations.feishu.oauth import DeviceFlowService
from app.repositories import (
    AgentRunRepository,
    BotEventRepository,
    RuntimeIncidentRepository,
)
from app.repositories_course_schedule import (
    CreateDraftOutcome,
    UnfillableScheduleContextError,
    prepare_schedule_context,
)
from app.presentation.contracts import (
    AgentActivityCallback,
    AgentActivityEvent,
    ResponsePlan,
    ResponseSegment,
    RuntimeResponse,
)
from app.presentation.progress_policy import should_force_silent_progress
from app.presentation.user_capabilities import help_text, onboarding_text
from app.presentation.progress_presenter import ProgressPresenter
from app.presentation.response_orchestrator import ResponseOrchestrator
from app.services.presentation_service import PresentationOutbox
from app.services.presentation_service import PendingImageCard
from app.services.course_schedule_vision import (
    CourseScheduleVisionError,
    CourseScheduleVisionUnavailable,
    CourseScheduleVisionValidationFailure,
)
from app.services.generic_image_vision import (
    GenericImageVisionError,
    GenericImageVisionUnavailable,
    GenericImageVisionValidationFailure,
)
from app.services.multimodal_turn_coordinator import (
    MultimodalInputSnapshot,
    MultimodalTurnCoordinator,
    PendingMultimodalTurn,
    RecentImageContext,
)


logger = logging.getLogger(__name__)
BIND_PATTERN = re.compile(r"^/bind(?:\s+(\S+))?\s*$", re.IGNORECASE)
CALENDAR_CONNECT_PATTERN = re.compile(
    r"^/(?:calendar|connect-calendar)\s*$", re.IGNORECASE
)
STOP_PATTERN = re.compile(r"^/stop\s*$", re.IGNORECASE)
HELP_PATTERN = re.compile(
    r"^(?:/help|帮助|功能|功能介绍|你能做什么|怎么用|怎么使用|MindFlow能做什么)[？?。！!\s]*$",
    re.IGNORECASE,
)
# These are infrastructure gates only. Natural-language intent is delegated to
# Generic Vision/Main Agent; this whitelist intentionally prefers precision.
EXPLICIT_SCHEDULE_IMPORT_FAST_PATHS = frozenset(
    {
        "把这张课表导入",
        "把这张课程表导入",
        "把这个课表导入",
        "把这个课程表导入",
        "把这张课表导入日历",
        "把这张课程表导入日历",
        "把这张课表导入飞书日历",
        "把这张课程表导入飞书日历",
        "把这个课表导入日历",
        "把这个课程表导入日历",
        "把这个课表导入飞书日历",
        "把这个课程表导入飞书日历",
    }
)
_EXPLICIT_RECENT_BARE_IMAGE_FOLLOWER = (
    r"(?=$|[\s，。！？,.!?；;：:]"
    r"|里|中|上|下|的|是|有|帮|请|再|看|怎|如|写|显|说|意|内|报|按)"
)
_EXPLICIT_RECENT_BARE_IMAGE_OBJECT = (
    r"(?:这|那)(?:张|幅)图" + _EXPLICIT_RECENT_BARE_IMAGE_FOLLOWER
)
_EXPLICIT_RECENT_NAMED_IMAGE_OBJECT = (
    r"(?:这|那)(?:个|张)图片"
    r"|(?:这|那)(?:个|张)截图"
    r"|(?:这|那)(?:个|份|张)课表"
    r"|(?:这|那)(?:个|份|张)课程表"
)
EXPLICIT_RECENT_IMAGE_REFERENCE_PATTERN = re.compile(
    rf"(?:"
    rf"{_EXPLICIT_RECENT_BARE_IMAGE_OBJECT}"
    rf"|{_EXPLICIT_RECENT_NAMED_IMAGE_OBJECT}"
    rf"|(?:刚才|刚刚)(?:"
    rf"{_EXPLICIT_RECENT_BARE_IMAGE_OBJECT}"
    rf"|{_EXPLICIT_RECENT_NAMED_IMAGE_OBJECT}"
    rf"|的(?:图{_EXPLICIT_RECENT_BARE_IMAGE_FOLLOWER}|图片|截图|课表|课程表)"
    rf")"
    rf")"
)
SCHEDULE_RECENT_FOLLOWUP_PATTERN = re.compile(
    r"^\s*(?:那)?周[一二三四五六日天]\s*(?:"
    r"呢\s*[？?]?"
    r"|(?:有|上)(?:什么|哪些)课\s*[？?]?"
    r"|几节(?:课)?\s*[？?]?"
    r"|几点(?:上课)?\s*[？?]?"
    r"|什么时候(?:上课)?\s*[？?]?"
    r")\s*$"
)
SCHEDULE_RECENT_IMPORT_FOLLOWUPS = frozenset(
    {
        "帮我导入这个",
        "帮我导入这个课表",
        "帮我导入这个课程表",
        "帮我导入一下",
        "帮我加进去",
        "加进去吧",
        "同步一下",
        "放进去",
    }
)
SCHEDULE_RECENT_IMPORT_INTENT_PATTERN = re.compile(
    r"(?:导入|添加|加入|加进|同步).{0,20}(?:课表|课程|日程)"
    r"|(?:课表|课程).{0,20}(?:导入|添加|加入|加进|同步)"
)
SCHEDULE_RECENT_IMPORT_NEGATION_PATTERN = re.compile(
    r"(?:不|先别|不要|无需|不用|取消).{0,8}(?:导入|添加|加入|加进|同步)"
)


@dataclass(frozen=True)
class ScheduleImageOutcome:
    status: Literal[
        "draft_created",
        "existing_draft",
        "preview_delivery_failed",
        "not_course_schedule",
        "failed",
    ]
    draft: dict | None
    image_kind: str


@dataclass(frozen=True)
class ScheduleReadOnlyOutcome:
    status: Literal["parsed", "not_course_schedule", "failed"]
    context: dict | None
    downloaded_image: object | None


@dataclass(frozen=True)
class AgentRunHandle:
    event_id: str
    run_id: object
    run_generation: int


@dataclass(frozen=True)
class RecentImageTaskHandle:
    event_id: str
    task: asyncio.Task
    stop_generation: int


def is_explicit_schedule_import_fast_path(text: str) -> bool:
    value = "".join(str(text or "").strip().split()).rstrip("。！？?!")
    return value in EXPLICIT_SCHEDULE_IMPORT_FAST_PATHS


def is_schedule_recent_import_request(text: str) -> bool:
    value = "".join(str(text or "").strip().split()).rstrip("。！？?!")
    if value in SCHEDULE_RECENT_IMPORT_FOLLOWUPS:
        return True
    # This route is reachable only for the sender's recent course-schedule
    # image.  It creates a reviewable draft, never a Calendar event.  Accept
    # normal direct requests such as “帮我加入上述课程，是重复性日程”, while
    # refusing any expressed negation.
    return bool(
        value
        and not SCHEDULE_RECENT_IMPORT_NEGATION_PATTERN.search(value)
        and SCHEDULE_RECENT_IMPORT_INTENT_PATTERN.search(value)
    )


class AgentRuntimeProtocol(Protocol):
    async def handle_message(
        self,
        ctx: AgentContext,
        turn_input: AgentTurnInput,
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
    final_ready: bool = False
    pending_tool_name: str | None = None
    pending_text: str | None = None
    pending_key: str | None = None
    tool_progress_generation: int = 0
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
        schedule_vision: object | None = None,
        generic_image_vision: object | None = None,
        schedule_imports: object | None = None,
        message_resources: object | None = None,
        schedule_draft_ttl_minutes: int = 60,
        schedule_image_max_concurrency: int = 1,
        multimodal_debounce_seconds: float = 3.0,
        multimodal_association_seconds: float = 15.0,
        multimodal_recent_context_seconds: float = 120.0,
        course_default_semester_start_date: str = "",
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
        self.schedule_vision = schedule_vision
        self.generic_image_vision = generic_image_vision
        self.schedule_imports = schedule_imports
        self.message_resources = message_resources
        self.schedule_draft_ttl_minutes = max(1, int(schedule_draft_ttl_minutes))
        self._schedule_image_semaphore = asyncio.Semaphore(
            max(1, int(schedule_image_max_concurrency))
        )
        self.multimodal_turns = MultimodalTurnCoordinator(
            debounce_seconds=multimodal_debounce_seconds,
            association_seconds=multimodal_association_seconds,
            recent_context_seconds=multimodal_recent_context_seconds,
        )
        self.course_default_semester_start_date = str(
            course_default_semester_start_date or ""
        ).strip()
        self._routing_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._active_agent_events: dict[
            object, dict[str, AgentRunHandle]
        ] = {}
        self._stop_generation: dict[object, int] = {}
        self._active_multimodal_tasks: dict[
            tuple[object, str],
            dict[str, tuple[PendingMultimodalTurn, asyncio.Task[None]]],
        ] = {}
        self._active_recent_image_tasks: dict[
            tuple[object, str], dict[str, RecentImageTaskHandle]
        ] = {}

    def _current_stop_generation(self, participant_id) -> int:
        return self._stop_generation.get(participant_id, 0)

    def _run_was_stopped(self, participant_id, run_generation: int) -> bool:
        return run_generation < self._current_stop_generation(participant_id)

    def _register_recent_image_task(
        self,
        participant_id,
        chat_id: str,
        event_id: str,
        task: asyncio.Task,
        stop_generation: int,
    ) -> tuple[object, str]:
        key = (participant_id, str(chat_id))
        handle = RecentImageTaskHandle(
            event_id=event_id,
            task=task,
            stop_generation=stop_generation,
        )
        self._active_recent_image_tasks.setdefault(key, {})[event_id] = handle
        return key

    def _unregister_recent_image_task(
        self,
        key: tuple[object, str],
        event_id: str,
        task: asyncio.Task,
    ) -> None:
        current = self._active_recent_image_tasks.get(key)
        if current is None:
            return
        registered = current.get(event_id)
        if registered is not None and registered.task is task:
            current.pop(event_id, None)
        if not current:
            self._active_recent_image_tasks.pop(key, None)

    async def _ensure_task_not_stopped(
        self,
        participant_id,
        event_id: str,
        task_generation: int,
    ) -> None:
        if not self._run_was_stopped(participant_id, task_generation):
            return
        await asyncio.to_thread(self.events.cancel_reply_plan, event_id)
        raise asyncio.CancelledError

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
        long_task: asyncio.Task | None = None
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
            bind_match = BIND_PATTERN.match(event.text) if event.message_type == "text" else None
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
                await self._deliver(event, onboarding_text(participant.participant_code))
                return
            if bind_match is not None:
                await self._deliver(event, "当前飞书账号已经绑定。")
                return
            if STOP_PATTERN.match(event.text):
                stop_generation = (
                    self._current_stop_generation(participant.id) + 1
                )
                self._stop_generation[participant.id] = stop_generation
                stopped_handles = [
                    handle
                    for handle in self._active_agent_events.get(
                        participant.id, {}
                    ).values()
                    if handle.run_generation < stop_generation
                ]
                for handle in stopped_handles:
                    await asyncio.to_thread(
                        self.events.cancel_reply_plan, handle.event_id
                    )
                interrupt = getattr(self.runtime, "interrupt", None)
                try:
                    runtime_stopped = (
                        await interrupt(participant.id) if interrupt else False
                    )
                    stopped = bool(stopped_handles) or runtime_stopped
                except Exception as exc:
                    stopped = True
                    logger.warning(
                        "agent_interrupt_failed participant_id=%s "
                        "event_id=%s error_class=%s",
                        participant.id,
                        event.event_id,
                        type(exc).__name__,
                    )
                multimodal_key = (participant.id, str(event.chat_id))
                image_tasks = self._active_multimodal_tasks.pop(
                    multimodal_key, {}
                )
                recent_image_tasks = self._active_recent_image_tasks.pop(
                    multimodal_key, {}
                )
                for active_turn, image_task in image_tasks.values():
                    if not image_task.done():
                        image_task.cancel()
                        stopped = True
                    await self.multimodal_turns.cancel(active_turn)
                    related_events = (
                        [active_turn.primary_image_event]
                        + list(active_turn.attached_text_events)
                        + list(active_turn.late_followups)
                    )
                    for related_event in related_events:
                        await asyncio.to_thread(
                            self.events.cancel_reply_plan,
                            related_event.event_id,
                        )
                for handle in recent_image_tasks.values():
                    if handle.stop_generation >= stop_generation:
                        continue
                    await asyncio.to_thread(
                        self.events.cancel_reply_plan, handle.event_id
                    )
                    if not handle.task.done():
                        handle.task.cancel()
                        stopped = True
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
            if event.message_type == "text" and HELP_PATTERN.match(event.text):
                await self._deliver(event, help_text())
                return
            if event.message_type == "text":
                attached = await self.multimodal_turns.attach_text(
                    participant.id, event.chat_id, event
                )
                if attached is not None:
                    is_late_followup = any(
                        item is event for item in attached.late_followups
                    )
                    await asyncio.to_thread(
                        self.events.save_telemetry,
                        event.event_id,
                        {
                            "multimodal_primary_event_id": (
                                attached.primary_image_event.event_id
                            ),
                            "multimodal_attached_to_event_id": (
                                attached.primary_image_event.event_id
                            ),
                            "multimodal_route": (
                                "late_image_followup"
                                if is_late_followup
                                else "attached_text"
                            ),
                        },
                    )
                    # The primary image task owns completion. Normal attached
                    # text is completed only after the frozen input has been
                    # consumed; late text is reprocessed as its own follow-up.
                    return

            if long_task is None and event.message_type == "text":
                recent = await self.multimodal_turns.recent_context(
                    participant.id, event.chat_id
                )
                explicit_image_reference = bool(
                    EXPLICIT_RECENT_IMAGE_REFERENCE_PATTERN.search(event.text)
                )
                schedule_followup = bool(
                    SCHEDULE_RECENT_FOLLOWUP_PATTERN.search(event.text)
                )
                schedule_import_followup = is_schedule_recent_import_request(
                    event.text
                )
                reuse_recent = bool(
                    recent is not None
                    and (
                        explicit_image_reference
                        or (schedule_followup and recent.image_kind == "course_schedule")
                        or (
                            schedule_import_followup
                            and recent.image_kind == "course_schedule"
                        )
                    )
                )
                if reuse_recent and recent is not None:
                    if participant.external_llm_consent_at is None:
                        await self._deliver(
                            event,
                            "目前还没有记录图片交给外部模型处理的授权，所以我暂时不能读取这张图片。请先联系研究者完成授权。",
                        )
                        return
                    task_generation = self._current_stop_generation(
                        participant.id
                    )
                    recent_task = asyncio.create_task(
                        self._handle_recent_image_text(
                            event,
                            participant,
                            recent,
                            task_generation=task_generation,
                        ),
                        name=f"recent-image-turn-{event.event_id}",
                    )
                    long_task = recent_task
                    recent_key = self._register_recent_image_task(
                        participant.id,
                        event.chat_id,
                        event.event_id,
                        recent_task,
                        task_generation,
                    )

                    def clear_recent_image(
                        done: asyncio.Task[RecentImageContext],
                    ) -> None:
                        self._unregister_recent_image_task(
                            recent_key, event.event_id, done
                        )

                    recent_task.add_done_callback(clear_recent_image)
            if long_task is not None:
                pass
            elif event.message_type == "image":
                if participant.external_llm_consent_at is None:
                    await self._deliver(
                        event,
                        "目前还没有记录图片交给外部模型处理的授权，所以我暂时不能读取这张图片。请先联系研究者完成授权。",
                    )
                    return
                opened_turn = await self.multimodal_turns.open_image(
                    participant.id, event.chat_id, event
                )
                task_generation = self._current_stop_generation(participant.id)
                long_task = asyncio.create_task(
                    self._process_multimodal_image(
                        opened_turn,
                        participant,
                        task_generation=task_generation,
                    ),
                    name=f"multimodal-turn-{event.event_id}",
                )
                multimodal_key = (participant.id, str(event.chat_id))
                participant_tasks = self._active_multimodal_tasks.setdefault(
                    multimodal_key, {}
                )
                participant_tasks[opened_turn.turn_id] = (opened_turn, long_task)

                def clear_multimodal(done: asyncio.Task[None]) -> None:
                    current = self._active_multimodal_tasks.get(multimodal_key)
                    if current is None:
                        return
                    registered = current.get(opened_turn.turn_id)
                    if registered is not None and registered[1] is done:
                        current.pop(opened_turn.turn_id, None)
                    if not current:
                        self._active_multimodal_tasks.pop(multimodal_key, None)

                long_task.add_done_callback(clear_multimodal)
            elif participant.external_llm_consent_at is None:
                await self._deliver(
                    event,
                    "尚未记录将本次对话发送给外部模型的实验授权，请先联系研究者。",
                )
                return

            elif long_task is None:
                skill = self.skill_loader.current()
                run_generation = self._current_stop_generation(participant.id)
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
                    turn_effect_policy="verify_on_demand",
                    user_request_text=event.text,
                    source_kind="text",
                )
                # Creating the task under the routing lock preserves arrival order;
                # the lock is released before the long Agent turn so /stop can pass.
                long_task = asyncio.create_task(
                    self._run_agent(
                        event, ctx, run_id, run_generation=run_generation
                    ),
                    name=f"agent-turn-{event.event_id}",
                )
                self._active_agent_events.setdefault(participant.id, {})[
                    event.event_id
                ] = AgentRunHandle(
                    event_id=event.event_id,
                    run_id=run_id,
                    run_generation=run_generation,
                )
        if long_task is not None:
            await long_task

    async def _process_multimodal_image(
        self,
        turn: PendingMultimodalTurn,
        participant,
        *,
        task_generation: int,
    ) -> None:
        event = turn.primary_image_event
        active = await self.multimodal_turns.wait_for_debounce(turn)
        if active is None:
            return
        route = "generic_image_agent"
        snapshot: MultimodalInputSnapshot | None = None
        recent: RecentImageContext | None = None
        consumption_finished = False
        dispatch_late = False
        try:
            snapshot = await self.multimodal_turns.freeze_or_snapshot_input(turn)
            attached = snapshot.text_events
            user_text = "\n".join(
                item
                for item in (
                    [event.text.strip()]
                    + [item.text.strip() for item in attached]
                )
                if item
            )
            if is_explicit_schedule_import_fast_path(user_text):
                route = "strict_schedule_fast_path"
                await self._note_multimodal_route(event, route)
                outcome = await self._handle_schedule_image(
                    event,
                    participant.id,
                    task_generation=task_generation,
                )
                await self._ensure_task_not_stopped(
                    participant.id, event.event_id, task_generation
                )
                if outcome.status in {"draft_created", "existing_draft"}:
                    recent = await self.multimodal_turns.complete(
                        turn,
                        image_message_id=event.message_id,
                        image_key=event.image_key,
                        image_kind=outcome.image_kind,
                        summary={
                            "route": route,
                            "draft_id": str((outcome.draft or {}).get("id") or ""),
                        },
                    )
                else:
                    await self.multimodal_turns.cancel(turn)
                consumption_finished = True
                dispatch_late = True
                return
            downloaded_image = None
            read_only = None
            if self.generic_image_vision is None or self.message_resources is None:
                await self._deliver(
                    event,
                    "这张图刚才没有读完整，你可以重发一次；如果方便，也可以告诉我你想让我重点看哪里。",
                )
                await self.multimodal_turns.cancel(turn)
                consumption_finished = True
                dispatch_late = True
                return
            async with self._schedule_image_semaphore:
                image = downloaded_image
                if image is None:
                    image = await self.message_resources.download_image(
                        event.message_id, str(event.image_key or "")
                    )
                context: GenericImageContext = await self.generic_image_vision.inspect(
                    image.data, image.mime_type, user_text=user_text
                )
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            if (
                context.image_kind == "course_schedule"
                and context.interaction_hint == "question"
            ):
                read_only = await self._parse_schedule_read_only(
                    event, downloaded_image=image
                )
                if read_only.status == "parsed" and read_only.context is not None:
                    schedule_context = read_only.context
                    route = "strict_schedule_read_only_after_generic"
                    await self._note_multimodal_route(event, route)
                    # Do not retain raw image bytes while the text-only Agent runs.
                    del read_only
                    del image
                    await self._run_agent_input(
                        event,
                        participant,
                        AgentTurnInput(
                            text=user_text,
                            trusted_image_context=schedule_context,
                        ),
                        calendar_mutation_policy="course_schedule_strict_only",
                        turn_effect_policy="read_compute_only",
                        source_kind="course_schedule_strict",
                        run_generation=task_generation,
                    )
                    await self._ensure_task_not_stopped(
                        participant.id, event.event_id, task_generation
                    )
                    recent = await self.multimodal_turns.complete(
                        turn,
                        image_message_id=event.message_id,
                        image_key=event.image_key,
                        image_kind="course_schedule",
                        summary=schedule_context,
                    )
                    consumption_finished = True
                    dispatch_late = True
                    return
            if (
                context.image_kind == "course_schedule"
                and context.interaction_hint == "course_import_request"
            ):
                route = "strict_schedule_after_image_kind"
                await self._note_multimodal_route(event, route)
                outcome = await self._handle_schedule_image(
                    event,
                    participant.id,
                    downloaded_image=image,
                    report_not_course_schedule=False,
                    task_generation=task_generation,
                )
                await self._ensure_task_not_stopped(
                    participant.id, event.event_id, task_generation
                )
                read_only = None
                downloaded_image = None
                del image
                if outcome.status in {"draft_created", "existing_draft"}:
                    recent = await self.multimodal_turns.complete(
                        turn,
                        image_message_id=event.message_id,
                        image_key=event.image_key,
                        image_kind=outcome.image_kind,
                        summary={
                            "route": route,
                            "draft_id": str((outcome.draft or {}).get("id") or ""),
                        },
                    )
                elif outcome.status == "not_course_schedule":
                    route = "generic_image_agent_after_strict_not_course_schedule"
                    await self._note_multimodal_route(event, route)
                    fallback_context = context.to_dict()
                    fallback_context["image_kind"] = outcome.image_kind
                    await self._run_agent_input(
                        event,
                        participant,
                        AgentTurnInput(
                            text=user_text,
                            trusted_image_context=fallback_context,
                        ),
                        calendar_mutation_policy="normal",
                        turn_effect_policy="verify_on_demand",
                        source_kind="generic_image",
                        run_generation=task_generation,
                    )
                    await self._ensure_task_not_stopped(
                        participant.id, event.event_id, task_generation
                    )
                    recent = await self.multimodal_turns.complete(
                        turn,
                        image_message_id=event.message_id,
                        image_key=event.image_key,
                        image_kind=outcome.image_kind,
                        summary=fallback_context,
                    )
                else:
                    await self.multimodal_turns.cancel(turn)
            else:
                read_only = None
                downloaded_image = None
                del image
                is_course_schedule_context = context.image_kind == "course_schedule"
                await self._note_multimodal_route(event, route)
                await self._run_agent_input(
                    event,
                    participant,
                    AgentTurnInput(
                        text=user_text,
                        trusted_image_context=context.to_dict(),
                    ),
                    calendar_mutation_policy=(
                        "course_schedule_strict_only"
                        if is_course_schedule_context
                        else "normal"
                    ),
                    turn_effect_policy=(
                        "read_compute_only"
                        if is_course_schedule_context
                        else "verify_on_demand"
                    ),
                    source_kind="generic_image",
                    run_generation=task_generation,
                )
                await self._ensure_task_not_stopped(
                    participant.id, event.event_id, task_generation
                )
                recent = await self.multimodal_turns.complete(
                    turn,
                    image_message_id=event.message_id,
                    image_key=event.image_key,
                    image_kind=context.image_kind,
                    summary=context.to_dict(),
                )
            consumption_finished = True
            dispatch_late = True
        except (MessageResourceTooLarge, UnsupportedImageFormat, ValueError):
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            await self._deliver(
                event, "无法处理这张图片，请使用大小合适的 JPEG、PNG 或 WebP 图片。"
            )
            await self.multimodal_turns.cancel(turn)
            consumption_finished = True
            dispatch_late = True
        except (
            GenericImageVisionUnavailable,
            GenericImageVisionValidationFailure,
            GenericImageVisionError,
            MessageResourceError,
        ):
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            await self._deliver(
                event,
                "这张图刚才没有读完整，你可以重发一次；如果方便，也可以告诉我你想让我重点看哪里。",
            )
            await self.multimodal_turns.cancel(turn)
            consumption_finished = True
            dispatch_late = True
        except Exception as exc:
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            logger.warning(
                "generic_image_processing_failed event_id=%s message_id=%s error_class=%s",
                event.event_id,
                event.message_id,
                type(exc).__name__,
            )
            await self._deliver(
                event,
                "这张图刚才没有读完整，你可以重发一次；如果方便，也可以告诉我你想让我重点看哪里。",
            )
            await self.multimodal_turns.cancel(turn)
            consumption_finished = True
            dispatch_late = True
        finally:
            if consumption_finished and snapshot is not None:
                await self._finish_consumed_text_events(
                    snapshot,
                    participant_id=participant.id,
                    task_generation=task_generation,
                )
            if dispatch_late:
                await self._dispatch_late_followups(
                    turn,
                    participant,
                    recent,
                    task_generation=task_generation,
                )

    async def _finish_consumed_text_events(
        self,
        snapshot: MultimodalInputSnapshot,
        *,
        participant_id,
        task_generation: int,
    ) -> None:
        for attached_event in snapshot.text_events:
            if self._run_was_stopped(participant_id, task_generation):
                return
            await asyncio.to_thread(
                self.events.finish, attached_event.event_id, status="completed"
            )

    async def _parse_schedule_read_only(
        self, event: BotEvent, *, downloaded_image: object | None = None
    ) -> ScheduleReadOnlyOutcome:
        if self.schedule_vision is None or self.message_resources is None:
            return ScheduleReadOnlyOutcome("failed", None, None)
        image = None
        try:
            async with self._schedule_image_semaphore:
                image = downloaded_image or await self.message_resources.download_image(
                    event.message_id, str(event.image_key or "")
                )
                result = await self.schedule_vision.parse(
                    image.data, image.mime_type
                )
            if result.document_type != "course_schedule":
                return ScheduleReadOnlyOutcome(
                    "not_course_schedule", None, image
                )
            structured = prepare_schedule_context(result)
            return ScheduleReadOnlyOutcome(
                "parsed",
                {
                    "image_kind": "course_schedule",
                    "route": "strict_schedule_read_only",
                    "schedule": structured,
                },
                image,
            )
        except (
            CourseScheduleVisionUnavailable,
            CourseScheduleVisionValidationFailure,
            CourseScheduleVisionError,
            MessageResourceError,
            MessageResourceTooLarge,
            UnsupportedImageFormat,
            ValueError,
        ) as exc:
            logger.warning(
                "course_schedule_read_only_failed event_id=%s message_id=%s error_class=%s",
                event.event_id,
                event.message_id,
                type(exc).__name__,
            )
            return ScheduleReadOnlyOutcome("failed", None, image)

    async def _dispatch_late_followups(
        self,
        turn: PendingMultimodalTurn,
        participant,
        recent: RecentImageContext | None,
        *,
        task_generation: int,
    ) -> None:
        current_recent = recent
        followups = await self.multimodal_turns.drain_late_followups(turn)
        current_task = asyncio.current_task()
        registrations: list[tuple[tuple[object, str], str]] = []
        if current_task is not None:
            for followup in followups:
                recent_key = self._register_recent_image_task(
                    participant.id,
                    followup.chat_id,
                    followup.event_id,
                    current_task,
                    task_generation,
                )
                registrations.append((recent_key, followup.event_id))
        try:
            for followup in followups:
                try:
                    if current_recent is not None:
                        current_recent = await self._handle_recent_image_text(
                            followup,
                            participant,
                            current_recent,
                            task_generation=task_generation,
                        )
                    else:
                        await self.process(followup)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "late_image_followup_failed event_id=%s error_class=%s",
                        followup.event_id,
                        type(exc).__name__,
                    )
                    await self._deliver(
                        followup,
                        "刚才补充的内容没有处理完整，请再发一次。",
                    )
        finally:
            if current_task is not None:
                for recent_key, event_id in registrations:
                    self._unregister_recent_image_task(
                        recent_key, event_id, current_task
                    )

    async def _handle_recent_image_text(
        self,
        event: BotEvent,
        participant,
        recent: RecentImageContext,
        *,
        task_generation: int,
    ) -> RecentImageContext:
        await self._ensure_task_not_stopped(
            participant.id, event.event_id, task_generation
        )
        trusted_context = dict(recent.structured_or_agent_summary)
        draft_id = str(trusted_context.get("draft_id") or "").strip()
        strict_read_only_followup = False
        if (
            recent.image_kind == "course_schedule"
            and bool(SCHEDULE_RECENT_FOLLOWUP_PATTERN.search(event.text))
            and not draft_id
            and trusted_context.get("route") != "strict_schedule_read_only"
        ):
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            source_event = BotEvent(
                event_id=event.event_id,
                message_id=recent.image_message_id,
                app_id=event.app_id,
                open_id=event.open_id,
                chat_id=event.chat_id,
                text=event.text,
                create_time=event.create_time,
                chat_type=event.chat_type,
                message_type="image",
                image_key=recent.image_key,
            )
            strict_context = await self._parse_schedule_read_only(source_event)
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            if (
                strict_context.status == "parsed"
                and strict_context.context is not None
            ):
                trusted_context = strict_context.context
                strict_read_only_followup = True
                strict_context = None
        if draft_id and self.schedule_imports is not None:
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            get_draft = getattr(self.schedule_imports.drafts, "get", None)
            draft = (
                await asyncio.to_thread(get_draft, draft_id)
                if callable(get_draft)
                else None
            )
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            if draft is not None:
                trusted_context = {
                    "image_kind": "course_schedule",
                    "route": trusted_context.get("route"),
                    "draft_id": draft_id,
                    "schedule": draft.get("structured_result"),
                    "items": draft.get("items"),
                }
        if is_schedule_recent_import_request(event.text):
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            source_event = BotEvent(
                event_id=event.event_id,
                message_id=recent.image_message_id,
                app_id=event.app_id,
                open_id=event.open_id,
                chat_id=event.chat_id,
                text=event.text,
                create_time=event.create_time,
                chat_type=event.chat_type,
                message_type="image",
                image_key=recent.image_key,
            )
            await self._note_multimodal_route(event, "recent_strict_schedule")
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            outcome = await self._handle_schedule_image(
                source_event,
                participant.id,
                delivery_event=event,
                task_generation=task_generation,
            )
            await self._ensure_task_not_stopped(
                participant.id, event.event_id, task_generation
            )
            if outcome.status in {"draft_created", "existing_draft"}:
                promoted = await self.multimodal_turns.promote_recent_context(
                    recent,
                    image_kind="course_schedule",
                    summary={
                        "image_kind": "course_schedule",
                        "route": "recent_strict_schedule",
                        "draft_id": str((outcome.draft or {}).get("id") or ""),
                    },
                )
                await self._ensure_task_not_stopped(
                    participant.id, event.event_id, task_generation
                )
                return promoted
            return recent
        await self._ensure_task_not_stopped(
            participant.id, event.event_id, task_generation
        )
        await self._note_multimodal_route(
            event,
            "recent_strict_schedule_read_only"
            if strict_read_only_followup
            else "recent_image_agent",
        )
        await self._ensure_task_not_stopped(
            participant.id, event.event_id, task_generation
        )
        await self._run_agent_input(
            event,
            participant,
            AgentTurnInput(
                text=event.text,
                trusted_image_context=trusted_context,
            ),
            calendar_mutation_policy=(
                "course_schedule_strict_only"
                if recent.image_kind == "course_schedule"
                else "normal"
            ),
            turn_effect_policy=(
                "verify_on_demand"
                if draft_id
                else "read_compute_only"
                if recent.image_kind == "course_schedule"
                else "verify_on_demand"
            ),
            source_kind=(
                "course_schedule_strict"
                if recent.image_kind == "course_schedule"
                else "generic_image"
            ),
            run_generation=task_generation,
        )
        await self._ensure_task_not_stopped(
            participant.id, event.event_id, task_generation
        )
        return recent

    async def _run_agent_input(
        self,
        event: BotEvent,
        participant,
        turn_input: AgentTurnInput,
        *,
        calendar_mutation_policy: CalendarMutationPolicy,
        turn_effect_policy: TurnEffectPolicy,
        source_kind: SourceKind,
        run_generation: int | None = None,
    ) -> None:
        if run_generation is None:
            run_generation = self._current_stop_generation(participant.id)
        if self._run_was_stopped(participant.id, run_generation):
            await asyncio.to_thread(
                self.events.cancel_reply_plan, event.event_id
            )
            return
        skill = self.skill_loader.current()
        start_task = asyncio.create_task(
            asyncio.to_thread(
                self.runs.start,
                participant.id,
                event.message_id,
                self.model,
                skill.version,
            ),
            name=f"agent-run-start-{event.event_id}",
        )
        try:
            run_id = await asyncio.shield(start_task)
        except asyncio.CancelledError as cancellation:
            try:
                run_id = await start_task
            except Exception:
                logger.warning(
                    "agent_run_start_failed_after_cancellation event_id=%s",
                    event.event_id,
                    exc_info=True,
                )
            else:
                await asyncio.to_thread(
                    self.runs.finish, run_id, "interrupted"
                )
                await asyncio.to_thread(
                    self.events.cancel_reply_plan, event.event_id
                )
            raise cancellation
        if self._run_was_stopped(participant.id, run_generation):
            await asyncio.to_thread(self.runs.finish, run_id, "interrupted")
            await asyncio.to_thread(
                self.events.cancel_reply_plan, event.event_id
            )
            return
        ctx = AgentContext(
            participant_id=participant.id,
            participant_code=participant.participant_code,
            open_id=event.open_id,
            chat_id=event.chat_id,
            message_id=event.message_id,
            agent_run_id=run_id,
            calendar_mutation_policy=calendar_mutation_policy,
            turn_effect_policy=turn_effect_policy,
            user_request_text=turn_input.text,
            source_kind=source_kind,
        )
        self._active_agent_events.setdefault(participant.id, {})[
            event.event_id
        ] = AgentRunHandle(
            event_id=event.event_id,
            run_id=run_id,
            run_generation=run_generation,
        )
        await self._run_agent(
            event,
            ctx,
            run_id,
            turn_input=turn_input,
            run_generation=run_generation,
        )

    async def _note_multimodal_route(self, event: BotEvent, route: str) -> None:
        await asyncio.to_thread(
            self.events.save_telemetry,
            event.event_id,
            {
                "multimodal_primary_event_id": event.event_id,
                "multimodal_route": route,
            },
        )

    async def _handle_schedule_image(
        self,
        event: BotEvent,
        participant_id,
        *,
        delivery_event: BotEvent | None = None,
        downloaded_image=None,
        report_not_course_schedule: bool = True,
        task_generation: int | None = None,
    ) -> ScheduleImageOutcome:
        delivery_event = delivery_event or event
        if task_generation is None:
            task_generation = self._current_stop_generation(participant_id)
        await self._ensure_task_not_stopped(
            participant_id, delivery_event.event_id, task_generation
        )
        if (
            self.schedule_vision is None
            or self.schedule_imports is None
            or (self.message_resources is None and downloaded_image is None)
        ):
            await self._ensure_task_not_stopped(
                participant_id, delivery_event.event_id, task_generation
            )
            await self._deliver(delivery_event, "这张课表刚才没有读完整，你可以直接重试一次。")
            return ScheduleImageOutcome("failed", None, "other")
        try:
            await self._ensure_task_not_stopped(
                participant_id, delivery_event.event_id, task_generation
            )
            existing = await asyncio.to_thread(
                self.schedule_imports.drafts.get_by_source,
                participant_id,
                event.message_id,
            )
            await self._ensure_task_not_stopped(
                participant_id, delivery_event.event_id, task_generation
            )
            if existing is not None:
                delivered = await self._deliver_card(
                    delivery_event, course_schedule_preview_card(existing)
                )
                await self._ensure_task_not_stopped(
                    participant_id, delivery_event.event_id, task_generation
                )
                if not delivered:
                    await self._report_schedule_preview_delivery_failure(
                        delivery_event
                    )
                    return ScheduleImageOutcome(
                        "preview_delivery_failed",
                        existing,
                        "course_schedule",
                    )
                return ScheduleImageOutcome(
                    "existing_draft", existing, "course_schedule"
                )
            async with self._schedule_image_semaphore:
                await self._ensure_task_not_stopped(
                    participant_id, delivery_event.event_id, task_generation
                )
                # Recheck after waiting so two deliveries of the same provider
                # event cannot both enter the expensive image pipeline.
                existing = await asyncio.to_thread(
                    self.schedule_imports.drafts.get_by_source,
                    participant_id,
                    event.message_id,
                )
                await self._ensure_task_not_stopped(
                    participant_id, delivery_event.event_id, task_generation
                )
                if existing is not None:
                    delivered = await self._deliver_card(
                        delivery_event, course_schedule_preview_card(existing)
                    )
                    await self._ensure_task_not_stopped(
                        participant_id, delivery_event.event_id, task_generation
                    )
                    if not delivered:
                        await self._report_schedule_preview_delivery_failure(
                            delivery_event
                        )
                        return ScheduleImageOutcome(
                            "preview_delivery_failed",
                            existing,
                            "course_schedule",
                        )
                    return ScheduleImageOutcome(
                        "existing_draft", existing, "course_schedule"
                    )
                image = downloaded_image
                if image is None:
                    image = await self.message_resources.download_image(
                        event.message_id, str(event.image_key or "")
                    )
                await self._ensure_task_not_stopped(
                    participant_id, delivery_event.event_id, task_generation
                )
                result = await self.schedule_vision.parse(
                    image.data, image.mime_type
                )
                await self._ensure_task_not_stopped(
                    participant_id, delivery_event.event_id, task_generation
                )
                if result.document_type != "course_schedule":
                    if report_not_course_schedule:
                        await self._deliver(
                            delivery_event,
                            "这张图看起来不像课程表。你如果想处理图里的其他内容，告诉我想做什么就行。",
                        )
                    return ScheduleImageOutcome(
                        "not_course_schedule", None, "other"
                    )
                create_outcome = getattr(
                    self.schedule_imports.drafts,
                    "create_draft_outcome",
                    None,
                )
                create_method = (
                    create_outcome
                    if callable(create_outcome)
                    else self.schedule_imports.drafts.create_draft
                )
                await self._ensure_task_not_stopped(
                    participant_id, delivery_event.event_id, task_generation
                )
                creation_task = asyncio.create_task(
                    asyncio.to_thread(
                        create_method,
                        participant_id,
                        source_message_id=event.message_id,
                        source_image_hash=hashlib.sha256(image.data).hexdigest(),
                        vision_model=self.schedule_vision.model,
                        result=result,
                        timezone_name=str(self.schedule_imports.timezone),
                        ttl_minutes=self.schedule_draft_ttl_minutes,
                        semester_start_date=(
                            date.fromisoformat(
                                self.course_default_semester_start_date
                            )
                            if self.course_default_semester_start_date
                            else None
                        ),
                    ),
                    name=f"course-draft-create-{event.event_id}",
                )
                try:
                    created = await asyncio.shield(creation_task)
                except asyncio.CancelledError as cancellation:
                    try:
                        created = await creation_task
                    except Exception:
                        logger.warning(
                            "course_draft_create_failed_after_stop "
                            "participant_id=%s event_id=%s",
                            participant_id,
                            event.event_id,
                            exc_info=True,
                        )
                    else:
                        if (
                            isinstance(created, CreateDraftOutcome)
                            and created.created_new
                        ):
                            await self._cancel_hidden_schedule_draft(
                                participant_id, created.draft
                            )
                    raise cancellation
                if isinstance(created, CreateDraftOutcome):
                    draft = created.draft
                    created_new = created.created_new
                else:
                    draft = created
                    created_new = True
                del image
                if self._run_was_stopped(participant_id, task_generation):
                    if created_new:
                        await self._cancel_hidden_schedule_draft(
                            participant_id, draft
                        )
                    await self._ensure_task_not_stopped(
                        participant_id,
                        delivery_event.event_id,
                        task_generation,
                    )
                try:
                    delivered = await self._deliver_card(
                        delivery_event, course_schedule_preview_card(draft)
                    )
                except asyncio.CancelledError:
                    if created_new:
                        await self._cancel_hidden_schedule_draft(
                            participant_id, draft
                        )
                    raise
                await self._ensure_task_not_stopped(
                    participant_id, delivery_event.event_id, task_generation
                )
                if not delivered:
                    if created_new:
                        await self._cancel_hidden_schedule_draft(
                            participant_id, draft
                        )
                    await self._report_schedule_preview_delivery_failure(
                        delivery_event
                    )
                    return ScheduleImageOutcome(
                        "preview_delivery_failed",
                        draft,
                        "course_schedule",
                    )
                return ScheduleImageOutcome(
                    "draft_created" if created_new else "existing_draft",
                    draft,
                    "course_schedule",
                )
        except UnfillableScheduleContextError as exc:
            await self._ensure_task_not_stopped(
                participant_id, delivery_event.event_id, task_generation
            )
            logger.info(
                "course_schedule_context_unfillable event_id=%s missing=%s",
                event.event_id,
                sorted(exc.missing),
            )
            await self._deliver(
                delivery_event, "这张课程表缺少星期、周次或可用时间，暂时无法可靠导入。请换一张信息更完整、清晰的图片。"
            )
            return ScheduleImageOutcome("failed", None, "other")

        except (MessageResourceTooLarge, UnsupportedImageFormat, ValueError) as exc:
            await self._ensure_task_not_stopped(
                participant_id, delivery_event.event_id, task_generation
            )
            logger.warning(
                "course_schedule_image_rejected event_id=%s message_id=%s error_class=%s",
                event.event_id,
                event.message_id,
                type(exc).__name__,
            )
            await self._deliver(
                delivery_event, "无法处理这张图片，请使用大小合适的 JPEG、PNG 或 WebP 图片。"
            )
            return ScheduleImageOutcome("failed", None, "other")
        except CourseScheduleVisionValidationFailure as exc:
            await self._ensure_task_not_stopped(
                participant_id, delivery_event.event_id, task_generation
            )
            logger.warning(
                "course_schedule_vision_validation_failed event_id=%s message_id=%s detail=%s",
                event.event_id,
                event.message_id,
                str(exc)[:160],
            )
            await self._deliver(
                delivery_event,
                "这张图已识别为课程表，但导入所需的结构化信息没有通过校验，"
                "所以还没有写入日历。请直接再试一次“导入这张课表”；"
                "如果仍失败，管理员可依据日志中的校验项定位问题。",
            )
            return ScheduleImageOutcome("failed", None, "other")
        except (CourseScheduleVisionUnavailable, CourseScheduleVisionError, MessageResourceError) as exc:
            await self._ensure_task_not_stopped(
                participant_id, delivery_event.event_id, task_generation
            )
            logger.warning(
                "course_schedule_image_service_unavailable event_id=%s message_id=%s error_class=%s",
                event.event_id,
                event.message_id,
                type(exc).__name__,
            )
            await self._deliver(delivery_event, "这张课表刚才没有读完整，你可以直接重试一次。")
            return ScheduleImageOutcome("failed", None, "other")
        except Exception as exc:
            await self._ensure_task_not_stopped(
                participant_id, delivery_event.event_id, task_generation
            )
            logger.warning(
                "course_schedule_image_processing_failed event_id=%s message_id=%s error_class=%s",
                event.event_id,
                event.message_id,
                type(exc).__name__,
            )
            await self._deliver(delivery_event, "这张课表刚才没有读完整，你可以直接重试一次。")
            return ScheduleImageOutcome("failed", None, "other")

    async def _report_schedule_preview_delivery_failure(
        self, delivery_event: BotEvent
    ) -> None:
        await self._deliver(
            delivery_event,
            "课程表已经识别出来了，但预览卡刚才没发成功。请稍后重新发送这张课程表。",
        )

    async def _cancel_hidden_schedule_draft(
        self, participant_id, draft: dict
    ) -> None:
        try:
            await asyncio.to_thread(
                self.schedule_imports.drafts.cancel,
                participant_id,
                draft["id"],
            )
        except Exception:
            logger.warning(
                "hidden_schedule_draft_cancel_failed participant_id=%s "
                "draft_id=%s",
                participant_id,
                draft.get("id"),
                exc_info=True,
            )

    async def _run_agent(
        self,
        event: BotEvent,
        ctx: AgentContext,
        run_id,
        *,
        turn_input: AgentTurnInput | None = None,
        run_generation: int,
    ) -> None:
        turn_input = turn_input or AgentTurnInput(text=event.text)
        started = time.monotonic()
        progress = ProgressState(
            force_silent=should_force_silent_progress(turn_input.text)
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
        generic_wakeup = asyncio.Event()

        async def emit_locked(text: str, *, key: str) -> None:
            """Send while holding progress.lock so final cannot overtake it."""

            now = time.monotonic()
            if progress.final_ready or self._run_was_stopped(
                ctx.participant_id, run_generation
            ):
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
            generic_wakeup.set()

        async def delayed_generic_progress() -> None:
            await asyncio.sleep(self.generic_progress_delay_seconds)
            while True:
                async with progress.lock:
                    if (
                        progress.final_ready
                        or progress.force_silent
                        or progress.sent
                    ):
                        return
                    contextual_owner_active = bool(
                        progress.pending_tool_name
                        and progress.pending_tool_name in progress.tool_started_at
                        and progress.pending_text
                    )
                    if not contextual_owner_active:
                        suggestion = self.progress_presenter.delayed(
                            turn_input.text, state=progress
                        )
                        if suggestion:
                            await emit_locked(suggestion, key="delayed")
                        return
                    # Clear under the same lock used by owner transitions so a
                    # completion wake-up cannot be lost between the state check
                    # and the wait below.
                    generic_wakeup.clear()
                await generic_wakeup.wait()

        async def delayed_tool_progress(
            expected_tool_name: str,
            expected_generation: int,
        ) -> None:
            await asyncio.sleep(self.tool_progress_grace_seconds)
            async with progress.lock:
                if (
                    progress.final_ready
                    or progress.tool_progress_generation != expected_generation
                    or progress.pending_tool_name != expected_tool_name
                    or expected_tool_name not in progress.tool_started_at
                ):
                    return
                suggestion = progress.pending_text
                key = progress.pending_key
                if suggestion and key:
                    await emit_locked(suggestion, key=key)

        tool_timer: asyncio.Task[None] | None = None
        tool_timers: set[asyncio.Task[None]] = set()

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
                    if progress.first_tool_started_at is None:
                        progress.first_tool_started_at = now
                    progress.tool_started_at[tool_name] = now
                elif activity.kind in {"tool_succeeded", "tool_failed"} and tool_name:
                    tool_started = progress.tool_started_at.pop(tool_name, None)
                    if tool_started is not None:
                        progress.tool_durations_ms.append(
                            round((now - tool_started) * 1000, 1)
                        )
                    if progress.pending_tool_name == tool_name:
                        progress.pending_tool_name = None
                        progress.pending_text = None
                        progress.pending_key = None
                        progress.tool_progress_generation += 1
                        if tool_timer is not None and not tool_timer.done():
                            tool_timer.cancel()
                        tool_timer = None
                        generic_wakeup.set()
                suggestion = self.progress_presenter.present(activity, state=progress)
                if suggestion:
                    key = self.progress_presenter.key_for(activity, state=progress)
                    progress.pending_tool_name = tool_name
                    progress.pending_text = suggestion
                    progress.pending_key = key
                    progress.tool_progress_generation += 1
                    if tool_timer is not None and not tool_timer.done():
                        tool_timer.cancel()
                    generation = progress.tool_progress_generation
                    tool_timer = asyncio.create_task(
                        delayed_tool_progress(tool_name, generation),
                        name=f"tool-progress-{event.event_id}-{generation}",
                    )
                    tool_timers.add(tool_timer)

        async def close_progress_before_final() -> None:
            # If a processing send already owns the lock, wait until the
            # provider call has completed. Otherwise mark final ready first so
            # a threshold-edge timer can no longer start a processing send.
            async with progress.lock:
                progress.final_ready = True
                generic_wakeup.set()
            generic_timer.cancel()
            timers = [generic_timer, *tool_timers]
            for pending_timer in tool_timers:
                pending_timer.cancel()
            await asyncio.gather(*timers, return_exceptions=True)

        generic_timer = asyncio.create_task(
            delayed_generic_progress(),
            name=f"generic-progress-{event.event_id}",
        )
        try:
            agent_started = time.monotonic()
            response = await self.runtime.handle_message(
                ctx,
                turn_input,
                chat_type=event.chat_type,
                on_activity=on_activity,
            )
            metrics["agent_result_ms"] = round(
                (time.monotonic() - agent_started) * 1000, 1
            )
            await close_progress_before_final()
            if self._run_was_stopped(ctx.participant_id, run_generation):
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
                if self._run_was_stopped(ctx.participant_id, run_generation):
                    raise ClaudeRuntimeInterrupted(FALLBACK_INTERRUPTED)
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
                        "error_code=%s retryable=%s error_class=%s "
                        "operation=%s error_message=%s",
                        event.event_id,
                        event.message_id,
                        exc.code,
                        exc.retryable,
                        exc.error_class,
                        exc.operation,
                        str(exc)[:1000],
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
            if self._run_was_stopped(ctx.participant_id, run_generation):
                raise ClaudeRuntimeInterrupted(FALLBACK_INTERRUPTED)
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
            if self._run_was_stopped(ctx.participant_id, run_generation):
                raise ClaudeRuntimeInterrupted(FALLBACK_INTERRUPTED)
            await asyncio.to_thread(self.runs.finish, run_id, "succeeded")
            delivered = await self._deliver_plan(
                event,
                plan,
                participant_id=ctx.participant_id,
                metrics=metrics,
                delivery_started_at=started,
                run_generation=run_generation,
            )
            if self._run_was_stopped(ctx.participant_id, run_generation):
                await asyncio.to_thread(self.runs.finish, run_id, "interrupted")
                await asyncio.to_thread(
                    self.events.cancel_reply_plan, event.event_id
                )
                status = "interrupted"
            else:
                status = "completed" if delivered else "reply_pending"
        except asyncio.CancelledError:
            await close_progress_before_final()
            if self.presentations is not None:
                self.presentations.discard(run_id)
            await asyncio.to_thread(self.runs.finish, run_id, "interrupted")
            if self._run_was_stopped(ctx.participant_id, run_generation):
                await asyncio.to_thread(
                    self.events.cancel_reply_plan, event.event_id
                )
            raise
        except ClaudeRuntimeInterrupted:
            await close_progress_before_final()
            if self.presentations is not None:
                self.presentations.discard(run_id)
            await asyncio.to_thread(self.runs.finish, run_id, "interrupted")
            if self._run_was_stopped(ctx.participant_id, run_generation):
                await asyncio.to_thread(
                    self.events.cancel_reply_plan, event.event_id
                )
                status = "interrupted"
            else:
                delivered = await self._deliver(event, FALLBACK_INTERRUPTED)
                status = "interrupted" if delivered else "reply_pending"
        except Exception:
            await close_progress_before_final()
            if self.presentations is not None:
                self.presentations.discard(run_id)
            if self._run_was_stopped(ctx.participant_id, run_generation):
                await asyncio.to_thread(self.runs.finish, run_id, "interrupted")
                await asyncio.to_thread(
                    self.events.cancel_reply_plan, event.event_id
                )
                status = "interrupted"
            else:
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
            timers = [generic_timer, *tool_timers]
            for pending_timer in tool_timers:
                pending_timer.cancel()
            await asyncio.gather(*timers, return_exceptions=True)
            participant_events = self._active_agent_events.get(
                ctx.participant_id
            )
            if participant_events is not None:
                handle = participant_events.get(event.event_id)
                if handle is not None and handle.run_id == run_id:
                    participant_events.pop(event.event_id, None)
                if not participant_events:
                    self._active_agent_events.pop(ctx.participant_id, None)
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

    async def _deliver_card(self, event: BotEvent, card: dict) -> bool:
        try:
            message_id = await self._send_card(
                event.chat_id,
                card,
                message_uuid=self._stable_message_uuid(
                    f"mindflow:card:{event.event_id}"
                ),
            )
        except FeishuSendError:
            await asyncio.to_thread(
                self.events.finish,
                event.event_id,
                status="received",
                error_code="card_send_failed",
            )
            return False
        await asyncio.to_thread(
            self.events.finish,
            event.event_id,
            status="completed",
            reply_message_id=message_id,
        )
        return True

    async def _deliver_plan(
        self,
        event: BotEvent,
        plan: ResponsePlan,
        *,
        participant_id=None,
        metrics: dict[str, object] | None = None,
        delivery_started_at: float | None = None,
        run_generation: int | None = None,
    ) -> bool:
        if (
            participant_id is not None
            and run_generation is not None
            and self._run_was_stopped(participant_id, run_generation)
        ):
            await asyncio.to_thread(
                self.events.cancel_reply_plan, event.event_id
            )
            return False
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
            run_generation=run_generation,
        )

    async def _resume_delivery_plan(
        self,
        event: BotEvent,
        pending_plan,
        *,
        participant_id=None,
        metrics: dict[str, object] | None = None,
        delivery_started_at: float | None = None,
        run_generation: int | None = None,
    ) -> bool:
        delivery_started = time.monotonic()
        first_final_recorded = False
        for index in range(pending_plan.next_segment, len(pending_plan.segments)):
            if (
                participant_id is not None
                and run_generation is not None
                and self._run_was_stopped(participant_id, run_generation)
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

    async def _send_card(
        self, chat_id: str, card: dict, *, message_uuid: str | None = None
    ) -> str:
        send_card = getattr(self.sender, "send_card", None)
        if not callable(send_card):
            raise FeishuSendError("Feishu card sending is unavailable", retryable=False)
        for attempt in range(self.max_retries + 1):
            try:
                if message_uuid and self._supports_card_message_uuid():
                    return await asyncio.to_thread(
                        send_card, chat_id, card, message_uuid=message_uuid
                    )
                return await asyncio.to_thread(send_card, chat_id, card)
            except FeishuSendError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    exc.attempt = attempt + 1
                    raise
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise FeishuSendError(FALLBACK_TEMPORARY)

    def _supports_card_message_uuid(self) -> bool:
        import inspect

        try:
            parameters = inspect.signature(self.sender.send_card).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.name == "message_uuid"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

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
        multimodal_tasks = {
            task
            for turns in self._active_multimodal_tasks.values()
            for _turn, task in turns.values()
        }
        recent_image_tasks = {
            handle.task
            for handles in self._active_recent_image_tasks.values()
            for handle in handles.values()
        }
        tasks = list(
            {
                *self._background_tasks,
                *multimodal_tasks,
                *recent_image_tasks,
            }
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close_orchestrator = getattr(self.response_orchestrator, "close", None)
        if callable(close_orchestrator):
            await close_orchestrator()
