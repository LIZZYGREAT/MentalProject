"""Short-lived association state for Feishu image and nearby text events."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


COLLECTING = "collecting"
PROCESSING = "processing"
COMPLETED = "completed"
CANCELLED = "cancelled"


@dataclass
class PendingMultimodalTurn:
    turn_id: str
    participant_id: Any
    chat_id: str
    primary_image_event: Any
    attached_text_events: list[Any]
    opened_at: float
    debounce_deadline: float
    association_deadline: float
    generation: int
    state: str = COLLECTING
    consumed_text_count: int = 0
    input_frozen: bool = False
    late_followups: list[Any] = field(default_factory=list)
    debounce_wakeup: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass(frozen=True)
class MultimodalInputSnapshot:
    """The exact nearby text committed to the next long model call."""

    text_events: tuple[Any, ...]


@dataclass(frozen=True)
class RecentImageContext:
    participant_id: Any
    chat_id: str
    image_message_id: str
    image_key: str | None
    image_kind: str
    structured_or_agent_summary: dict[str, Any]
    created_at: float
    expires_at: float
    association_deadline: float
    generation: int


@dataclass(frozen=True)
class OpenImageResult:
    turn: PendingMultimodalTurn
    displaced_turn: PendingMultimodalTurn | None = None


class MultimodalTurnCoordinator:
    """Coordinates ephemeral image turns without persisting raw media."""

    def __init__(
        self,
        *,
        debounce_seconds: float = 3.0,
        association_seconds: float = 15.0,
        recent_context_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.association_seconds = max(
            self.debounce_seconds, float(association_seconds)
        )
        self.recent_context_seconds = max(0.0, float(recent_context_seconds))
        self._clock = clock
        self._pending: dict[tuple[Any, str], PendingMultimodalTurn] = {}
        self._recent: dict[tuple[Any, str], RecentImageContext] = {}
        self._latest_generation: dict[tuple[Any, str], int] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def key(participant_id: Any, chat_id: str) -> tuple[Any, str]:
        return participant_id, str(chat_id)

    async def open_image(
        self, participant_id: Any, chat_id: str, image_event: Any
    ) -> OpenImageResult:
        now = self._clock()
        key = self.key(participant_id, chat_id)
        async with self._lock:
            generation = self._latest_generation.get(key, 0) + 1
            self._latest_generation[key] = generation
            self._recent.pop(key, None)
            displaced = self._pending.get(key)
            if displaced is not None and displaced.state in {COLLECTING, PROCESSING}:
                # A second image is a new turn. Wake the first as image-only
                # instead of silently dropping it or aggregating both images.
                displaced.state = PROCESSING
                displaced.debounce_wakeup.set()
            else:
                displaced = None
            turn = PendingMultimodalTurn(
                turn_id=uuid.uuid4().hex,
                participant_id=participant_id,
                chat_id=str(chat_id),
                primary_image_event=image_event,
                attached_text_events=[],
                opened_at=now,
                debounce_deadline=now + self.debounce_seconds,
                association_deadline=now + self.association_seconds,
                generation=generation,
            )
            self._pending[key] = turn
            return OpenImageResult(turn=turn, displaced_turn=displaced)

    async def attach_text(
        self, participant_id: Any, chat_id: str, text_event: Any
    ) -> PendingMultimodalTurn | None:
        now = self._clock()
        key = self.key(participant_id, chat_id)
        async with self._lock:
            turn = self._pending.get(key)
            if (
                turn is None
                or turn.state not in {COLLECTING, PROCESSING}
                or now > turn.association_deadline
            ):
                return None
            if turn.input_frozen:
                turn.late_followups.append(text_event)
            else:
                turn.attached_text_events.append(text_event)
            return turn

    async def wait_for_debounce(
        self, turn: PendingMultimodalTurn
    ) -> PendingMultimodalTurn | None:
        delay = max(0.0, turn.debounce_deadline - self._clock())
        if delay:
            try:
                await asyncio.wait_for(turn.debounce_wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        key = self.key(turn.participant_id, turn.chat_id)
        async with self._lock:
            current = self._pending.get(key)
            if current is not turn:
                return turn if turn.state == PROCESSING else None
            if turn.state != COLLECTING:
                return None
            turn.state = PROCESSING
            return turn

    async def attached_texts(self, turn: PendingMultimodalTurn) -> tuple[Any, ...]:
        async with self._lock:
            return tuple(turn.attached_text_events)

    async def freeze_or_snapshot_input(
        self, turn: PendingMultimodalTurn
    ) -> MultimodalInputSnapshot:
        """Freeze the text payload before a long call starts.

        Text arriving after this point is deliberately queued as a follow-up
        instead of being acknowledged as part of an input the model cannot see.
        """

        async with self._lock:
            turn.input_frozen = True
            turn.consumed_text_count = len(turn.attached_text_events)
            return MultimodalInputSnapshot(
                text_events=tuple(
                    turn.attached_text_events[: turn.consumed_text_count]
                )
            )

    async def drain_late_followups(
        self, turn: PendingMultimodalTurn
    ) -> tuple[Any, ...]:
        async with self._lock:
            events = tuple(turn.late_followups)
            turn.late_followups.clear()
            return events

    async def active_turn(
        self, participant_id: Any, chat_id: str
    ) -> PendingMultimodalTurn | None:
        now = self._clock()
        key = self.key(participant_id, chat_id)
        async with self._lock:
            turn = self._pending.get(key)
            if turn is None or turn.state not in {COLLECTING, PROCESSING}:
                return None
            if now > turn.association_deadline:
                return None
            return turn

    async def complete(
        self,
        turn: PendingMultimodalTurn,
        *,
        image_message_id: str,
        image_key: str | None,
        image_kind: str,
        summary: dict[str, Any],
    ) -> RecentImageContext:
        now = self._clock()
        recent = RecentImageContext(
            participant_id=turn.participant_id,
            chat_id=turn.chat_id,
            image_message_id=str(image_message_id),
            image_key=image_key,
            image_kind=str(image_kind),
            structured_or_agent_summary=dict(summary),
            created_at=now,
            expires_at=now + self.recent_context_seconds,
            association_deadline=turn.association_deadline,
            generation=turn.generation,
        )
        key = self.key(turn.participant_id, turn.chat_id)
        async with self._lock:
            turn.state = COMPLETED
            if self._pending.get(key) is turn:
                self._pending.pop(key, None)
            if turn.generation == self._latest_generation.get(key):
                self._recent[key] = recent
        return recent

    async def promote_recent_context(
        self,
        recent: RecentImageContext,
        *,
        image_kind: str,
        summary: dict[str, Any],
    ) -> RecentImageContext:
        """Upgrade one image context without reviving an older generation."""

        promoted = RecentImageContext(
            participant_id=recent.participant_id,
            chat_id=recent.chat_id,
            image_message_id=recent.image_message_id,
            image_key=recent.image_key,
            image_kind=str(image_kind),
            structured_or_agent_summary=dict(summary),
            created_at=recent.created_at,
            expires_at=recent.expires_at,
            association_deadline=recent.association_deadline,
            generation=recent.generation,
        )
        key = self.key(recent.participant_id, recent.chat_id)
        async with self._lock:
            if recent.generation == self._latest_generation.get(key):
                self._recent[key] = promoted
        return promoted

    async def cancel(self, turn: PendingMultimodalTurn) -> None:
        key = self.key(turn.participant_id, turn.chat_id)
        async with self._lock:
            turn.state = CANCELLED
            if self._pending.get(key) is turn:
                self._pending.pop(key, None)

    async def recent_context(
        self, participant_id: Any, chat_id: str
    ) -> RecentImageContext | None:
        now = self._clock()
        key = self.key(participant_id, chat_id)
        async with self._lock:
            recent = self._recent.get(key)
            if recent is None:
                return None
            if now > recent.expires_at:
                self._recent.pop(key, None)
                return None
            return recent
