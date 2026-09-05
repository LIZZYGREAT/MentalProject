"""Backend-owned durable ingress fed by an isolated Feishu receiver process."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import multiprocessing
from queue import Empty
from typing import Any, Callable

from app.identity.service import IdentityService
from app.repositories import BotEventRepository


logger = logging.getLogger(__name__)


class InvalidBotEvent(ValueError):
    pass


class FeishuReceiverError(RuntimeError):
    """The isolated WebSocket receiver failed or exited unexpectedly."""


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
    message_type: str = "text"
    image_key: str | None = None

    def to_ipc_payload(self) -> dict[str, str]:
        """Return a stable, SDK-free payload suitable for process IPC."""

        payload = {
            "event_id": self.event_id,
            "message_id": self.message_id,
            "app_id": self.app_id,
            "open_id": self.open_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "message_type": self.message_type,
            "text": self.text,
            "create_time": self.create_time.astimezone(timezone.utc).isoformat(),
        }
        if self.message_type == "image":
            payload["image_key"] = self.image_key or ""
        return payload

    @classmethod
    def from_ipc_payload(cls, payload: dict[str, Any]) -> "BotEvent":
        message_type = str(payload.get("message_type") or "").strip()
        if message_type not in {"text", "image"}:
            raise InvalidBotEvent("unsupported IPC message type")
        required = (
            "event_id",
            "message_id",
            "app_id",
            "open_id",
            "chat_id",
            "create_time",
        )
        values = {name: str(payload.get(name) or "").strip() for name in required}
        if not all(values.values()):
            raise InvalidBotEvent("IPC event is missing required fields")
        text = str(payload.get("text") or "").strip()
        image_key = str(payload.get("image_key") or "").strip()
        if message_type == "text" and not text:
            raise InvalidBotEvent("IPC text event is empty")
        if message_type == "image" and not image_key:
            raise InvalidBotEvent("IPC image event has no image_key")
        try:
            created = datetime.fromisoformat(values["create_time"])
        except ValueError as exc:
            raise InvalidBotEvent("IPC event has invalid create_time") from exc
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return cls(
            event_id=values["event_id"],
            message_id=values["message_id"],
            app_id=values["app_id"],
            open_id=values["open_id"],
            chat_id=values["chat_id"],
            create_time=created.astimezone(timezone.utc),
            chat_type=str(payload.get("chat_type") or "p2p"),
            message_type=message_type,
            text=text[:4000],
            image_key=image_key or None,
        )


@dataclass(frozen=True)
class CardActionEvent:
    event_id: str
    message_id: str
    app_id: str
    open_id: str
    chat_id: str
    action_tag: str
    action_value: dict[str, Any]
    form_value: dict[str, Any]

    def to_ipc_payload(self) -> dict[str, Any]:
        """Return the SDK-free CardAction contract shared with the receiver."""

        return {
            "event_id": self.event_id,
            "message_id": self.message_id,
            "app_id": self.app_id,
            "open_id": self.open_id,
            "chat_id": self.chat_id,
            "action_tag": self.action_tag,
            "action_value": dict(self.action_value),
            "form_value": dict(self.form_value),
        }

    @classmethod
    def from_ipc_payload(cls, payload: dict[str, Any]) -> "CardActionEvent":
        required = ("event_id", "message_id", "app_id", "open_id", "chat_id")
        values = {name: str(payload.get(name) or "").strip() for name in required}
        action_value = payload.get("action_value")
        form_value = payload.get("form_value")
        if not all(values.values()):
            raise InvalidBotEvent("card action IPC event is missing routing fields")
        if not isinstance(action_value, dict) or not isinstance(form_value, dict):
            raise InvalidBotEvent("card action IPC values must be objects")
        return cls(
            event_id=values["event_id"],
            message_id=values["message_id"],
            app_id=values["app_id"],
            open_id=values["open_id"],
            chat_id=values["chat_id"],
            action_tag=str(payload.get("action_tag") or "")[:64],
            action_value=dict(action_value),
            form_value=dict(form_value),
        )


class FeishuCardActionAdapter:
    def __init__(self, app_id: str):
        self.app_id = app_id

    def adapt(self, event: Any) -> CardActionEvent:
        return self._build(
            callback_event_id=getattr(event, "event_id", ""),
            message_id=getattr(event, "message_id", ""),
            chat_id=getattr(event, "chat_id", ""),
            open_id=getattr(getattr(event, "operator", None), "open_id", ""),
            action=getattr(event, "action", None),
        )

    def adapt_p2(self, callback: Any) -> CardActionEvent:
        event = getattr(callback, "event", None)
        context = getattr(event, "context", None)
        return self._build(
            callback_event_id=getattr(getattr(callback, "header", None), "event_id", ""),
            message_id=getattr(context, "open_message_id", ""),
            chat_id=getattr(context, "open_chat_id", ""),
            open_id=getattr(getattr(event, "operator", None), "open_id", ""),
            action=getattr(event, "action", None),
        )

    def _build(
        self, *, callback_event_id: Any, message_id: Any, chat_id: Any,
        open_id: Any, action: Any
    ) -> CardActionEvent:
        message_id = str(message_id or "").strip()
        chat_id = str(chat_id or "").strip()
        open_id = str(open_id or "").strip()
        tag = str(getattr(action, "tag", "") or "")[:64]
        value = getattr(action, "value", None) or {}
        form_value = getattr(action, "form_value", None) or {}
        if not all((message_id, chat_id, open_id)):
            raise InvalidBotEvent("card action is missing routing fields")
        if not isinstance(value, dict) or not isinstance(form_value, dict):
            raise InvalidBotEvent("card action values must be objects")
        identity_payload = json.dumps(
            {
                "message_id": message_id,
                "open_id": open_id,
                "tag": tag,
                "value": value,
                "form_value": form_value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        provider_event_id = str(callback_event_id or "").strip()
        event_id = provider_event_id or (
            "card:" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
        )
        return CardActionEvent(
            event_id=event_id,
            message_id=message_id,
            app_id=self.app_id,
            open_id=open_id,
            chat_id=chat_id,
            action_tag=tag,
            action_value=dict(value),
            form_value=dict(form_value),
        )


class FeishuChannelCardActionAdapter(FeishuCardActionAdapter):
    """Map the Channel SDK CardActionEvent while retaining its raw event id."""

    def adapt(self, event: Any) -> CardActionEvent:
        raw = getattr(event, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        header = raw.get("header") or {}
        provider_event_id = (
            header.get("event_id")
            if isinstance(header, dict)
            else None
        ) or raw.get("event_id")
        return self._build(
            callback_event_id=provider_event_id,
            message_id=getattr(event, "message_id", ""),
            chat_id=getattr(event, "chat_id", ""),
            open_id=getattr(getattr(event, "operator", None), "open_id", ""),
            action=getattr(event, "action", None),
        )


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
        message_type = str(message.get("message_type") or "")
        if message_type not in {"text", "image"}:
            raise InvalidBotEvent("unsupported message type")
        content = message.get("content") or "{}"
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise InvalidBotEvent("invalid text message content") from exc
        text = str((content or {}).get("text") or "").strip()
        image_key = str((content or {}).get("image_key") or "").strip()
        if message_type == "text" and not text:
            raise InvalidBotEvent("empty text message")
        if message_type == "image" and not image_key:
            raise InvalidBotEvent("image message has no image_key")
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
            create_time=created,
            chat_type=str(message.get("chat_type") or "p2p"),
            message_type=message_type,
            text=text[:4000],
            image_key=image_key or None,
        )


class FeishuChannelMessageAdapter:
    """Map lark_channel inbound messages without weakening event identity."""

    def __init__(self, app_id: str):
        self.app_id = app_id

    def adapt(self, message: Any) -> BotEvent:
        sender = getattr(message, "sender", None)
        conversation = getattr(message, "conversation", None)
        if sender is None or conversation is None:
            raise InvalidBotEvent("channel message is missing routing objects")
        if bool(getattr(sender, "is_bot", False)):
            raise InvalidBotEvent("bot self-message")
        message_type = str(getattr(message, "raw_content_type", ""))
        if message_type not in {"text", "image"}:
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
        resources = list(getattr(message, "resources", None) or [])
        image_key = ""
        if message_type == "image":
            image_key = next(
                (
                    str(getattr(resource, "file_key", "") or "").strip()
                    for resource in resources
                    if str(getattr(resource, "type", "") or "") == "image"
                ),
                "",
            )
            if not image_key:
                content = getattr(message, "content", None)
                image_key = str(getattr(content, "image_key", "") or "").strip()
        if message_type == "text" and not text:
            raise InvalidBotEvent("empty text message")
        if message_type == "image" and not image_key:
            raise InvalidBotEvent("image message has no image_key")
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
            create_time=created,
            chat_type=str(getattr(conversation, "chat_type", "p2p") or "p2p"),
            message_type=message_type,
            text=text[:4000] if message_type == "text" else "",
            image_key=image_key or None,
        )


class FeishuGateway:
    """Own a spawned receiver and keep SDK lifecycle outside the backend loop."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        identity: IdentityService,
        events: BotEventRepository,
        queue: asyncio.Queue[BotEvent],
        channel_factory: Callable[..., Any] | None = None,
        *,
        process_context: Any | None = None,
        receiver_target: Callable[..., None] | None = None,
        start_timeout_seconds: float = 30.0,
        stop_timeout_seconds: float = 8.0,
        device_flow_close_timeout_seconds: float = 8.0,
        channel_sdk_version: str | None = None,
        card_action_handler: Callable[[CardActionEvent], Any] | None = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.identity = identity
        self.events = events
        self.queue = queue
        self.parser = FeishuEventParser(app_id)
        self.channel_adapter = FeishuChannelMessageAdapter(app_id)
        self.channel_factory = channel_factory
        self.process_context = process_context
        self.receiver_target = receiver_target
        self.start_timeout_seconds = start_timeout_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self.device_flow_close_timeout_seconds = device_flow_close_timeout_seconds
        self.channel_sdk_version = channel_sdk_version
        self.card_action_handler = card_action_handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._process: Any | None = None
        self._process_started = False
        self._output_queue: Any | None = None
        self._stop_event: Any | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._closed: asyncio.Future[None] | None = None
        self._stopping = False
        self._shutdown_failure: BaseException | None = None

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
        if event.app_id != self.app_id:
            logger.debug(
                "feishu_gateway_event_rejected event_id=%s message_id=%s reason=app_mismatch",
                event.event_id,
                event.message_id,
            )
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
            message_type=event.message_type,
            text=event.text,
            image_key=event.image_key,
            create_time=event.create_time,
        ):
            logger.debug(
                "feishu_gateway_event_duplicate event_id=%s message_id=%s",
                event.event_id,
                event.message_id,
            )
            return False
        try:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._enqueue, event)
            else:
                self._enqueue(event)
        except RuntimeError:
            self.events.finish(event.event_id, status="failed", error_code="queue_unavailable")
            logger.error(
                "feishu_gateway_event_failed event_id=%s message_id=%s "
                "error_code=queue_unavailable",
                event.event_id,
                event.message_id,
            )
            return False
        logger.info(
            "feishu_gateway_event_accepted event_id=%s message_id=%s "
            "chat_type=%s accepted=true",
            event.event_id,
            event.message_id,
            event.chat_type,
        )
        return True

    def _enqueue(self, event: BotEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Keep the durable event recoverable on the next process start.
            self.events.finish(event.event_id, status="received", error_code="queue_full")
            logger.warning(
                "feishu_event_queue_full event_id=%s message_id=%s",
                event.event_id,
                event.message_id,
            )

    @property
    def is_running(self) -> bool:
        return bool(self._process is not None and self._process.is_alive())

    async def start(self) -> None:
        """Spawn the receiver and return only after its WebSocket is ready."""

        if self._process is not None:
            raise RuntimeError("FeishuGateway is already started")
        from app.integrations.feishu.receiver_process import receiver_process_main

        self._loop = asyncio.get_running_loop()
        context = self.process_context or multiprocessing.get_context("spawn")
        target = self.receiver_target or receiver_process_main
        self._output_queue = context.Queue()
        self._stop_event = context.Event()
        self._ready = self._loop.create_future()
        self._closed = self._loop.create_future()
        self._stopping = False
        self._shutdown_failure = None
        self._process = context.Process(
            name="feishu-ws-receiver",
            target=target,
            args=(
                self.app_id,
                self.app_secret,
                self._output_queue,
                self._stop_event,
                self.channel_factory,
                self.device_flow_close_timeout_seconds,
                self.channel_sdk_version,
                self.card_action_handler is not None,
            ),
            daemon=True,
        )
        try:
            self._process.start()
            self._process_started = True
            self._consumer_task = asyncio.create_task(
                self._consume_receiver_output(), name="feishu-receiver-ipc"
            )
            await asyncio.wait_for(
                asyncio.shield(self._ready), timeout=self.start_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise FeishuReceiverError(
                f"Feishu receiver did not become ready within "
                f"{self.start_timeout_seconds:g} seconds"
            ) from exc
        except BaseException:
            await self.stop()
            raise
        logger.info(
            "feishu_receiver_ready receiver_pid=%s",
            getattr(self._process, "pid", None),
        )

    async def wait_closed(self) -> None:
        """Wait for receiver termination and surface an abnormal exit."""

        if self._closed is None:
            raise RuntimeError("FeishuGateway has not been started")
        await asyncio.shield(self._closed)

    async def stop(self) -> None:
        """Stop and reap the receiver, propagating any shutdown failure."""

        process = self._process
        if process is None:
            return
        closed = self._closed
        forced_termination = False
        terminal_state_timed_out = False
        self._stopping = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._process_started:
            await asyncio.to_thread(process.join, self.stop_timeout_seconds)
            if process.is_alive():
                logger.warning("feishu_receiver_graceful_stop_timed_out")
                forced_termination = True
                process.terminate()
                await asyncio.to_thread(process.join, 2.0)
        if self._consumer_task is not None and not self._consumer_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._consumer_task), timeout=1.0
                )
            except asyncio.TimeoutError:
                terminal_state_timed_out = True
                self._consumer_task.cancel()
                await asyncio.gather(self._consumer_task, return_exceptions=True)
        if closed is not None and not closed.done():
            if forced_termination:
                self._shutdown_failure = FeishuReceiverError(
                    "Feishu receiver required force termination during shutdown"
                )
                closed.set_exception(self._shutdown_failure)
            elif terminal_state_timed_out:
                self._shutdown_failure = FeishuReceiverError(
                    "Feishu receiver exited without a terminal IPC state"
                )
                closed.set_exception(self._shutdown_failure)
            elif process.exitcode not in (None, 0):
                self._shutdown_failure = FeishuReceiverError(
                    f"Feishu receiver stopped with code {process.exitcode}"
                )
                closed.set_exception(self._shutdown_failure)
            else:
                self._shutdown_failure = FeishuReceiverError(
                    "Feishu receiver exited without confirming clean shutdown"
                )
                closed.set_exception(self._shutdown_failure)
        if self._ready is not None:
            if not self._ready.done():
                self._ready.cancel()
            try:
                self._ready.result()
            except BaseException:
                # A shutdown error can also complete readiness after start()
                # timed out. Retrieve that state before releasing the Future.
                pass
        if closed is not None:
            try:
                closed.result()
            except asyncio.CancelledError as exc:
                if self._shutdown_failure is None:
                    self._shutdown_failure = FeishuReceiverError(
                        "Feishu receiver shutdown state was cancelled"
                    )
                    self._shutdown_failure.__cause__ = exc
            except BaseException:
                # Explicitly retrieve the Future exception. Runtime failures are
                # surfaced by wait_closed(); only failures during stop re-raise here.
                pass
        output_queue = self._output_queue
        if output_queue is not None:
            close = getattr(output_queue, "close", None)
            if callable(close):
                close()
            join_thread = getattr(output_queue, "join_thread", None)
            if callable(join_thread):
                await asyncio.to_thread(join_thread)
        self._process = None
        self._process_started = False
        self._output_queue = None
        self._stop_event = None
        self._consumer_task = None
        self._loop = None
        self._ready = None
        self._closed = None
        logger.info("feishu_receiver_stopped")
        shutdown_failure = self._shutdown_failure
        self._shutdown_failure = None
        if shutdown_failure is not None:
            raise shutdown_failure

    async def _consume_receiver_output(self) -> None:
        assert self._output_queue is not None
        while True:
            try:
                envelope = await asyncio.to_thread(
                    self._output_queue.get, True, 0.2
                )
            except Empty:
                process = self._process
                if process is not None and self._process_started and not process.is_alive():
                    if not self._stopping:
                        self._receiver_failed(
                            f"Feishu receiver exited unexpectedly with code "
                            f"{process.exitcode}"
                        )
                        return
                    # A clean shutdown is confirmed only by an explicit terminal
                    # envelope; Queue feeder delivery may lag process exit.
                    continue
                continue
            except (EOFError, OSError) as exc:
                if not self._stopping:
                    self._receiver_failed(f"Feishu receiver IPC failed: {exc}")
                return
            if not isinstance(envelope, dict):
                logger.warning("feishu_receiver_invalid_envelope")
                continue
            kind = envelope.get("kind")
            if kind == "ready":
                if self._ready is not None and not self._ready.done():
                    self._ready.set_result(None)
                continue
            if kind == "event":
                try:
                    event = BotEvent.from_ipc_payload(envelope.get("payload") or {})
                except InvalidBotEvent:
                    logger.warning("feishu_receiver_invalid_event")
                    continue
                logger.info(
                    "feishu_gateway_ipc_event_received event_id=%s message_id=%s "
                    "chat_type=%s",
                    event.event_id,
                    event.message_id,
                    event.chat_type,
                )
                self.accept_event(event)
                continue
            if kind == "card_action":
                try:
                    event = CardActionEvent.from_ipc_payload(
                        envelope.get("payload") or {}
                    )
                except InvalidBotEvent:
                    logger.warning("feishu_receiver_invalid_card_action")
                    continue
                if event.app_id != self.app_id:
                    logger.warning(
                        "feishu_receiver_card_action_app_mismatch "
                        "event_id=%s message_id=%s",
                        event.event_id,
                        event.message_id,
                    )
                    continue
                logger.info(
                    "feishu_gateway_ipc_card_action_received "
                    "event_id=%s message_id=%s",
                    event.event_id,
                    event.message_id,
                )
                if self.card_action_handler is None:
                    logger.warning(
                        "feishu_gateway_card_action_unhandled "
                        "event_id=%s message_id=%s",
                        event.event_id,
                        event.message_id,
                    )
                    continue
                try:
                    await asyncio.to_thread(self.card_action_handler, event)
                except Exception:
                    logger.exception(
                        "feishu_gateway_card_action_failed "
                        "event_id=%s message_id=%s",
                        event.event_id,
                        event.message_id,
                    )
                continue
            if kind == "error":
                error_type = str(envelope.get("error_type") or "ReceiverError")
                message = str(envelope.get("message") or "unknown receiver failure")
                self._receiver_failed(f"{error_type}: {message}")
                return
            if kind == "stopped":
                if self._stopping:
                    if self._closed is not None and not self._closed.done():
                        self._closed.set_result(None)
                else:
                    self._receiver_failed("Feishu receiver stopped unexpectedly")
                return

    def _receiver_failed(self, message: str) -> None:
        error = FeishuReceiverError(message)
        logger.error("feishu_receiver_failed", extra={"receiver_error": message})
        if self._stopping:
            self._shutdown_failure = error
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(error)
        if self._closed is not None and not self._closed.done():
            self._closed.set_exception(error)
