"""Single-App, single-WebSocket ingress with fast durable deduplication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, Callable

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


class FeishuChannelMessageAdapter:
    """Map lark_oapi.channel.InboundMessage without weakening event identity."""

    def __init__(self, app_id: str):
        self.app_id = app_id

    def adapt(self, message: Any) -> BotEvent:
        sender = getattr(message, "sender", None)
        conversation = getattr(message, "conversation", None)
        if sender is None or conversation is None:
            raise InvalidBotEvent("channel message is missing routing objects")
        if bool(getattr(sender, "is_bot", False)):
            raise InvalidBotEvent("bot self-message")
        if str(getattr(message, "raw_content_type", "")) != "text":
            raise InvalidBotEvent("unsupported message type")
        message_id = str(getattr(message, "id", "") or "").strip()
        open_id = str(getattr(sender, "open_id", "") or "").strip()
        chat_id = str(getattr(conversation, "chat_id", "") or "").strip()
        raw = getattr(message, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        # Channel normalization retains the raw message, not always the event
        # header. The stable Feishu message_id is the deterministic fallback.
        event_id = str(
            raw.get("event_id")
            or (raw.get("header") or {}).get("event_id")
            or message_id
        ).strip()
        if not all((event_id, message_id, open_id, chat_id)):
            raise InvalidBotEvent("channel message is missing routing fields")
        text = str(getattr(message, "content_text", "") or "").strip()
        if not text:
            raise InvalidBotEvent("empty text message")
        raw_time = getattr(message, "create_time", None)
        try:
            timestamp = int(raw_time)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            created = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            created = datetime.now(timezone.utc)
        return BotEvent(
            event_id=event_id,
            message_id=message_id,
            app_id=self.app_id,
            open_id=open_id,
            chat_id=chat_id,
            text=text[:4000],
            create_time=created,
            chat_type=str(getattr(conversation, "chat_type", "p2p") or "p2p"),
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
        channel_factory: Callable[..., Any] | None = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.identity = identity
        self.events = events
        self.queue = queue
        self.parser = FeishuEventParser(app_id)
        self.channel_adapter = FeishuChannelMessageAdapter(app_id)
        self.channel_factory = channel_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._channel: Any | None = None

    def accept_payload(self, payload: dict[str, Any]) -> bool:
        """Safe to call from the SDK callback thread; never waits for an Agent."""

        try:
            event = self.parser.parse(payload)
        except InvalidBotEvent:
            return False
        return self.accept_event(event)

    def accept_channel_message(self, message: Any) -> bool:
        try:
            event = self.channel_adapter.adapt(message)
        except InvalidBotEvent:
            return False
        return self.accept_event(event)

    def accept_event(self, event: BotEvent) -> bool:
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
        """Run one official Channel lifecycle without nested event loops."""

        if self.channel_factory is None:
            from lark_channel import FeishuChannel

            factory = FeishuChannel
        else:
            factory = self.channel_factory
        self._loop = asyncio.get_running_loop()

        async def on_message(message: Any) -> None:
            try:
                self.accept_channel_message(message)
            except Exception:
                logger.exception("feishu_event_callback_failed")

        channel = factory(app_id=self.app_id, app_secret=self.app_secret)
        self._channel = channel
        channel.on("message", on_message)
        try:
            await channel.connect()
        finally:
            disconnect = getattr(channel, "disconnect", None)
            if disconnect is not None:
                await disconnect()
            self._channel = None
