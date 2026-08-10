"""Single-App, single-WebSocket ingress with fast durable deduplication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any

from app.identity.service import IdentityService
from app.repositories import BotEventRepository


logger = logging.getLogger(__name__)


class InvalidBotEvent(ValueError):
    pass


@dataclass(frozen=True)
class BotEvent:
    event_id: str
    message_id: str
    app_id: str
    open_id: str
    chat_id: str
    text: str
    create_time: datetime
    chat_type: str = "p2p"


class FeishuEventParser:
    def __init__(self, app_id: str):
        self.app_id = app_id

    def parse(self, payload: dict[str, Any]) -> BotEvent:
        header = payload.get("header") or {}
        event = payload.get("event") or payload
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        if str(sender.get("sender_type") or "").lower() in {"app", "bot"}:
            raise InvalidBotEvent("bot self-message")
        open_id = sender_id.get("open_id") or sender.get("open_id")
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        event_id = header.get("event_id") or payload.get("event_id") or message_id
        if not all((event_id, message_id, open_id, chat_id)):
            raise InvalidBotEvent("message event is missing routing fields")
        if str(message.get("message_type") or "") != "text":
            raise InvalidBotEvent("unsupported message type")
        content = message.get("content") or "{}"
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise InvalidBotEvent("invalid text message content") from exc
        text = str((content or {}).get("text") or "").strip()
        if not text:
            raise InvalidBotEvent("empty text message")
        raw_time = message.get("create_time") or header.get("create_time")
        try:
            timestamp = int(raw_time)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            created = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            created = datetime.now(timezone.utc)
        return BotEvent(
            event_id=str(event_id),
            message_id=str(message_id),
            app_id=self.app_id,
            open_id=str(open_id),
            chat_id=str(chat_id),
            text=text[:4000],
            create_time=created,
            chat_type=str(message.get("chat_type") or "p2p"),
        )


class FeishuGateway:
    """Callback performs parsing, identity lookup, dedupe, persistence, enqueue only."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        identity: IdentityService,
        events: BotEventRepository,
        queue: asyncio.Queue[BotEvent],
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.identity = identity
        self.events = events
        self.queue = queue
        self.parser = FeishuEventParser(app_id)
        self._loop: asyncio.AbstractEventLoop | None = None

    def accept_payload(self, payload: dict[str, Any]) -> bool:
        """Safe to call from the SDK callback thread; never waits for an Agent."""

        try:
            event = self.parser.parse(payload)
        except InvalidBotEvent:
            return False
        participant = self.identity.resolve(event.app_id, event.open_id)
        participant_id = participant.id if participant else None
        if not self.events.accept(
            event.event_id,
            event.message_id,
            participant_id,
            app_id=event.app_id,
            open_id=event.open_id,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            text=event.text,
            create_time=event.create_time,
        ):
            return False
        try:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._enqueue, event)
            else:
                self._enqueue(event)
        except RuntimeError:
            self.events.finish(event.event_id, status="failed", error_code="queue_unavailable")
            return False
        return True

    def _enqueue(self, event: BotEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Keep the durable event recoverable on the next process start.
            self.events.finish(event.event_id, status="received", error_code="queue_full")

    async def start(self) -> None:
        """Start exactly one SDK WebSocket receiver and block until it exits."""

        import lark_oapi as lark

        self._loop = asyncio.get_running_loop()

        def on_message(data: Any) -> None:
            try:
                raw = lark.JSON.marshal(data)
                payload = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(payload, dict):
                    self.accept_payload(payload)
            except Exception:
                logger.exception("feishu_event_callback_failed")

        handler = (
            lark.EventDispatcherHandler.builder("", "", lark.LogLevel.WARNING)
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )
        client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )
        await asyncio.to_thread(client.start)
