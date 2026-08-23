"""Participant-scoped queues and bounded warm ClaudeSDKClient sessions."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from app.agent.context import AgentContext
from app.agent.sdk_adapter import (
    ClaudeClient,
    ClaudeClientFactory,
    ClaudeSDKInvocationError,
    ClaudeSDKTurnInterrupted,
    ClaudeTurnResult,
    ToolProgressCallback,
)
from app.agent.sdk_mcp import TurnContextBinding
from app.presentation.contracts import (
    AgentActivityCallback,
    AgentActivityEvent,
)
from app.repositories import ClaudeSessionRepository


class ParticipantQueueFull(ClaudeSDKInvocationError):
    pass


@dataclass
class TurnRequest:
    ctx: AgentContext
    text: str
    on_activity: AgentActivityCallback | None
    future: asyncio.Future[ClaudeTurnResult]


@dataclass
class ParticipantAgentSession:
    participant_id: uuid.UUID
    queue: asyncio.Queue[TurnRequest]
    binding: TurnContextBinding = field(default_factory=TurnContextBinding)
    client: ClaudeClient | None = None
    processing_task: asyncio.Task[None] | None = None
    active_request: TurnRequest | None = None
    state: str = "idle"
    last_active_at: float = field(default_factory=time.monotonic)


class ParticipantSessionManager:
    def __init__(
        self,
        factory: ClaudeClientFactory,
        repository: ClaudeSessionRepository,
        *,
        max_active_sessions: int = 2,
        idle_timeout_seconds: int = 120,
        turn_timeout_seconds: int = 90,
        input_queue_size: int = 20,
    ):
        self.factory = factory
        self.repository = repository
        self.max_active_sessions = max(1, int(max_active_sessions))
        self.idle_timeout_seconds = max(1, int(idle_timeout_seconds))
        self.turn_timeout_seconds = max(1, int(turn_timeout_seconds))
        self.input_queue_size = max(1, int(input_queue_size))
        self._sessions: dict[uuid.UUID, ParticipantAgentSession] = {}
        self._connecting: set[uuid.UUID] = set()
        self._lock = asyncio.Lock()
        self._closing = False

    async def submit(
        self,
        ctx: AgentContext,
        text: str,
        *,
        on_activity: AgentActivityCallback | None = None,
        on_tool_use: ToolProgressCallback | None = None,
    ) -> ClaudeTurnResult:
        if self._closing:
            raise ClaudeSDKInvocationError("session manager is closing")
        loop = asyncio.get_running_loop()
        async with self._lock:
            session = self._sessions.get(ctx.participant_id)
            if session is None:
                session = ParticipantAgentSession(
                    participant_id=ctx.participant_id,
                    queue=asyncio.Queue(maxsize=self.input_queue_size),
                )
                self._sessions[ctx.participant_id] = session
            if on_activity is None and on_tool_use is not None:
                async def legacy_activity(event: AgentActivityEvent) -> None:
                    if event.kind == "tool_started" and event.tool_name:
                        await on_tool_use(event.tool_name)

                on_activity = legacy_activity
            request = TurnRequest(
                ctx=ctx,
                text=str(text),
                on_activity=on_activity,
                future=loop.create_future(),
            )
            try:
                session.queue.put_nowait(request)
            except asyncio.QueueFull as exc:
                raise ParticipantQueueFull("participant input queue is full") from exc
            if session.processing_task is None or session.processing_task.done():
                session.processing_task = asyncio.create_task(
                    self._process_session(session),
                    name=f"claude-session-{ctx.participant_id}",
                )
        return await request.future

    async def interrupt(self, participant_id: uuid.UUID) -> bool:
        async with self._lock:
            session = self._sessions.get(participant_id)
            if (
                session is None
                or session.client is None
                or session.active_request is None
                or session.state not in {"running", "interrupting"}
            ):
                return False
            session.state = "interrupting"
            client = session.client
        await client.interrupt()
        return True

    async def _process_session(self, session: ParticipantAgentSession) -> None:
        try:
            while not self._closing:
                try:
                    request = await asyncio.wait_for(
                        session.queue.get(), timeout=self.idle_timeout_seconds
                    )
                except asyncio.TimeoutError:
                    # Retire atomically with submit(). A new request either
                    # keeps this worker alive or creates a fresh session after
                    # this one is removed; it can never be stranded in a queue
                    # whose worker is exiting.
                    async with self._lock:
                        if not session.queue.empty():
                            continue
                        if self._sessions.get(session.participant_id) is session:
                            self._sessions.pop(session.participant_id, None)
                        session.state = "closed"
                    break
                session.active_request = request
                try:
                    client = await self._ensure_client(session)
                    session.binding.current = request.ctx
                    session.binding.activity_callback = request.on_activity
                    async with self._lock:
                        session.state = "running"

                    async def legacy_tool_started(tool_name: str) -> None:
                        await session.binding.emit(
                            AgentActivityEvent(
                                kind="tool_started", tool_name=tool_name
                            )
                        )

                    result = await asyncio.wait_for(
                        client.run_turn(request.text, legacy_tool_started),
                        timeout=self.turn_timeout_seconds,
                    )
                    self.repository.save(
                        session.participant_id,
                        result.session_id,
                        last_message_id=request.ctx.message_id,
                    )
                    if not request.future.done():
                        request.future.set_result(result)
                except asyncio.TimeoutError as exc:
                    await self._interrupt_and_drop(session)
                    self.repository.mark_stale(session.participant_id)
                    if not request.future.done():
                        request.future.set_exception(
                            ClaudeSDKInvocationError("Claude turn timed out")
                        )
                except ClaudeSDKTurnInterrupted as exc:
                    await self._drop_client(session)
                    if not request.future.done():
                        request.future.set_exception(exc)
                except Exception as exc:
                    await self._drop_client(session)
                    if not request.future.done():
                        request.future.set_exception(
                            exc
                            if isinstance(exc, ClaudeSDKInvocationError)
                            else ClaudeSDKInvocationError(type(exc).__name__)
                        )
                finally:
                    session.binding.current = None
                    session.binding.activity_callback = None
                    session.active_request = None
                    session.last_active_at = time.monotonic()
                    async with self._lock:
                        if session.state != "closed":
                            session.state = "idle"
                    session.queue.task_done()
        finally:
            await self._drop_client(session)
            async with self._lock:
                if self._sessions.get(session.participant_id) is session:
                    self._sessions.pop(session.participant_id, None)

    async def _ensure_client(
        self, session: ParticipantAgentSession
    ) -> ClaudeClient:
        if session.client is not None:
            return session.client
        victim_client: ClaudeClient | None = None
        while True:
            async with self._lock:
                if session.client is not None:
                    return session.client
                connected = sum(
                    1 for item in self._sessions.values() if item.client is not None
                )
                occupied = connected + len(self._connecting)
                if occupied < self.max_active_sessions:
                    self._connecting.add(session.participant_id)
                    session.state = "connecting"
                    break
                candidates = [
                    item
                    for item in self._sessions.values()
                    if item is not session
                    and item.client is not None
                    and item.active_request is None
                    and item.queue.empty()
                    and item.state == "idle"
                ]
                if candidates:
                    victim = min(candidates, key=lambda item: item.last_active_at)
                    victim_client = victim.client
                    victim.client = None
                    victim.state = "closed"
                    self._connecting.add(session.participant_id)
                    session.state = "connecting"
                    break
            await asyncio.sleep(0.05)
        if victim_client is not None:
            await victim_client.disconnect()
        saved = self.repository.get(session.participant_id)
        resume_id = saved.session_id if saved and saved.status == "active" else None
        client = self.factory.create(session.binding, resume_session_id=resume_id)
        try:
            await client.connect()
        except Exception:
            async with self._lock:
                self._connecting.discard(session.participant_id)
                session.state = "closed"
            if resume_id:
                self.repository.mark_stale(session.participant_id)
            raise
        async with self._lock:
            self._connecting.discard(session.participant_id)
            session.client = client
            session.state = "idle"
        return client

    async def _interrupt_and_drop(self, session: ParticipantAgentSession) -> None:
        client = session.client
        if client is not None:
            try:
                await client.interrupt()
            except Exception:
                pass
        await self._drop_client(session)

    async def _drop_client(self, session: ParticipantAgentSession) -> None:
        async with self._lock:
            client = session.client
            session.client = None
            self._connecting.discard(session.participant_id)
            session.state = "closed"
        if client is not None:
            await client.disconnect()

    async def close(self) -> None:
        self._closing = True
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.client is not None and session.active_request is not None:
                try:
                    await session.client.interrupt()
                except Exception:
                    pass
        tasks = [
            session.processing_task
            for session in sessions
            if session.processing_task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
