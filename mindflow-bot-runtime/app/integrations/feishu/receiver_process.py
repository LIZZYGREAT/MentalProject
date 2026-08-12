"""Process entry point that exclusively owns lark-channel-sdk lifecycle."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable


def _safe_error_message(error: BaseException, secret: str) -> str:
    message = str(error) or type(error).__name__
    return message.replace(secret, "[redacted]") if secret else message


def _drain_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel SDK-owned cron/ping/select tasks before closing its loop."""

    if loop.is_closed():
        return
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def receiver_process_main(
    app_id: str,
    app_secret: str,
    output_queue: Any,
    stop_event: Any,
    channel_factory: Callable[..., Any] | None = None,
    device_flow_close_timeout_seconds: float = 8.0,
    channel_sdk_version: str | None = None,
) -> None:
    """Run FeishuChannel in a fresh interpreter with a process-local loop."""

    # lark_channel.ws.client captures asyncio.get_event_loop() at import time.
    # Establish the receiver-owned loop before the lazy SDK import.
    receiver_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(receiver_loop)
    from app.logging_security import install_credential_redaction

    install_credential_redaction()

    if channel_factory is None:
        from lark_channel import FeishuChannel

        channel_factory = FeishuChannel

    from app.integrations.feishu.gateway import (
        FeishuChannelMessageAdapter,
        InvalidBotEvent,
    )
    from app.integrations.feishu.channel_shutdown import stop_feishu_channel_cleanly

    adapter = FeishuChannelMessageAdapter(app_id)
    channel: Any | None = None
    stop_lock = threading.Lock()
    stop_started = False
    stop_finished = threading.Event()
    stop_errors: list[BaseException] = []
    monitor: threading.Thread | None = None
    lifecycle_error: BaseException | None = None

    def stop_channel_once(*, propagate_error: bool = False) -> None:
        """Start SDK teardown once and never return before it has finished."""

        nonlocal stop_started
        with stop_lock:
            if channel is None:
                return
            owns_stop = not stop_started
            if owns_stop:
                stop_started = True
        if owns_stop:
            try:
                stop_feishu_channel_cleanly(
                    channel,
                    device_flow_close_timeout_seconds=(
                        device_flow_close_timeout_seconds
                    ),
                    sdk_version=channel_sdk_version,
                )
            except BaseException as exc:
                stop_errors.append(exc)
            finally:
                stop_finished.set()
        else:
            stop_finished.wait()
        if propagate_error and stop_errors:
            raise stop_errors[0]

    try:
        channel = channel_factory(app_id=app_id, app_secret=app_secret)

        def on_message(message: Any) -> None:
            try:
                event = adapter.adapt(message)
            except InvalidBotEvent:
                return
            output_queue.put({"kind": "event", "payload": event.to_ipc_payload()})

        channel.on("message", on_message)

        def monitor_lifecycle() -> None:
            ready_sent = False
            while not stop_event.wait(0.05):
                ws_client = getattr(channel, "ws_client", None)
                transport_connected = bool(
                    ws_client is not None and getattr(ws_client, "_conn", None) is not None
                )
                if not ready_sent and (
                    bool(getattr(channel, "is_ready", False)) or transport_connected
                ):
                    output_queue.put({"kind": "ready"})
                    ready_sent = True
            stop_channel_once()

        monitor = threading.Thread(
            target=monitor_lifecycle,
            name="feishu-receiver-lifecycle",
            daemon=True,
        )
        monitor.start()
        channel.start()
        if not stop_event.is_set():
            raise RuntimeError("FeishuChannel.start returned unexpectedly")
    except BaseException as exc:
        if not stop_event.is_set():
            lifecycle_error = exc
    finally:
        requested = stop_event.is_set()
        stop_event.set()
        try:
            stop_channel_once(propagate_error=True)
        except BaseException as exc:
            if lifecycle_error is None:
                lifecycle_error = exc
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join()
        _drain_loop(receiver_loop)
        receiver_loop.close()
        if lifecycle_error is not None:
            output_queue.put(
                {
                    "kind": "error",
                    "error_type": type(lifecycle_error).__name__,
                    "message": _safe_error_message(lifecycle_error, app_secret),
                }
            )
        else:
            output_queue.put({"kind": "stopped", "requested": requested})
