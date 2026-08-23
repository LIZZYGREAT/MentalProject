"""Narrow shutdown compatibility for lark-channel-sdk 1.2.0 receivers."""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError, TimeoutError as FutureTimeoutError
from importlib.metadata import PackageNotFoundError, version
import logging
import threading
from typing import Any


logger = logging.getLogger(__name__)

_COMPATIBLE_SDK_VERSION = "1.2.0"


class DeviceFlowCloseError(RuntimeError):
    """The SDK DeviceFlow HTTP client could not be closed safely."""


class SdkBackgroundShutdownError(RuntimeError):
    """The SDK background event loop did not finish a clean shutdown."""


def _installed_sdk_version() -> str | None:
    try:
        return version("lark-channel-sdk")
    except PackageNotFoundError:
        return None


def _cancel_and_retrieve(
    future: Any,
    completed: threading.Event,
    *,
    timeout_seconds: float,
) -> None:
    future.cancel()
    if not completed.wait(timeout_seconds):
        raise DeviceFlowCloseError(
            "Feishu DeviceFlow close cancellation did not finish"
        )
    try:
        future.result()
    except CancelledError:
        pass


def _prepare_ws_shutdown(channel: Any) -> None:
    """Prevent reconnect and retrieve the receive task's terminal exception."""

    ws_client = getattr(channel, "_ws_client", None)
    if ws_client is None:
        return
    if hasattr(ws_client, "_auto_reconnect"):
        ws_client._auto_reconnect = False

    ws_loop = getattr(ws_client, "_loop", None)
    if (
        ws_loop is None
        or not callable(getattr(ws_loop, "is_running", None))
        or not ws_loop.is_running()
    ):
        return

    def retrieve_receive_task() -> None:
        for task in asyncio.all_tasks(ws_loop):
            coroutine_name = getattr(task.get_coro(), "__qualname__", "")
            if not coroutine_name.endswith("Client._receive_message_loop"):
                continue

            def retrieve_result(completed_task: asyncio.Task[Any]) -> None:
                try:
                    completed_task.exception()
                except BaseException:
                    pass

            task.add_done_callback(retrieve_result)

    try:
        ws_loop.call_soon_threadsafe(retrieve_receive_task)
    except RuntimeError:
        # The receiver loop won the shutdown race and is already stopping.
        pass


def _drain_running_loop(
    loop: asyncio.AbstractEventLoop,
    *,
    timeout_seconds: float,
) -> None:
    """Cancel and retrieve every task on the SDK's dedicated background loop."""

    completed = threading.Event()

    async def drain() -> None:
        try:
            current = asyncio.current_task()
            while True:
                pending = [
                    task
                    for task in asyncio.all_tasks()
                    if task is not current and not task.done()
                ]
                if not pending:
                    break
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            completed.set()

    drain_coroutine = drain()
    try:
        drain_future = asyncio.run_coroutine_threadsafe(drain_coroutine, loop)
    except BaseException:
        drain_coroutine.close()
        raise
    try:
        drain_future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        _cancel_and_retrieve(
            drain_future,
            completed,
            timeout_seconds=timeout_seconds,
        )
        raise SdkBackgroundShutdownError(
            "Feishu SDK background tasks did not drain within "
            f"{timeout_seconds:g} seconds"
        ) from exc
    except BaseException as exc:
        raise SdkBackgroundShutdownError(
            "Feishu SDK background task cleanup failed"
        ) from exc


def _verify_background_stopped(
    loop: asyncio.AbstractEventLoop,
    thread: threading.Thread | None,
) -> None:
    if thread is not None and thread.is_alive():
        raise SdkBackgroundShutdownError(
            "Feishu SDK background thread remained alive after channel.stop()"
        )
    if loop.is_running():
        raise SdkBackgroundShutdownError(
            "Feishu SDK background loop remained running after channel.stop()"
        )
    if not loop.is_closed():
        # Version 1.2.0 owns this loop exclusively. Closing it here also makes
        # teardown deterministic if a compatible build stops the thread but
        # omits loop.close(). Tasks were already gathered above.
        loop.close()


def stop_feishu_channel_cleanly(
    channel: Any,
    *,
    device_flow_close_timeout_seconds: float,
    sdk_version: str | None = None,
) -> None:
    """Fully drain the SDK-owned loop and then invoke its public stop API."""

    resolved_version = sdk_version if sdk_version is not None else _installed_sdk_version()
    if resolved_version != _COMPATIBLE_SDK_VERSION:
        logger.warning(
            "feishu_channel_shutdown_compatibility_skipped",
            extra={
                "installed_sdk_version": resolved_version or "not-installed",
                "compatible_sdk_version": _COMPATIBLE_SDK_VERSION,
            },
        )
        channel.stop()
        return

    bg_loop = getattr(channel, "_bg_loop", None)
    bg_thread = getattr(channel, "_bg_thread", None)
    device_flow = getattr(channel, "_device_flow", None)
    close = getattr(device_flow, "close", None)
    _prepare_ws_shutdown(channel)
    if (
        bg_loop is None
        or not callable(getattr(bg_loop, "is_running", None))
        or not bg_loop.is_running()
        or not callable(close)
    ):
        # Test doubles and future SDK objects without the 1.2.0 private
        # DeviceFlow shape must still receive their public stop signal.  The
        # compatibility pre-close is an optimization, not a reason to skip the
        # only operation that can unblock channel.start().
        channel.stop()
        return

    completed = threading.Event()

    async def close_device_flow() -> None:
        try:
            await close()
        finally:
            completed.set()

    shutdown_error: BaseException | None = None
    close_future: Any | None = None
    close_coroutine = close_device_flow()
    try:
        close_future = asyncio.run_coroutine_threadsafe(close_coroutine, bg_loop)
    except BaseException as exc:
        close_coroutine.close()
        shutdown_error = DeviceFlowCloseError(
            "Feishu DeviceFlow close could not be scheduled"
        )
        shutdown_error.__cause__ = exc
    if close_future is not None:
        try:
            close_future.result(timeout=device_flow_close_timeout_seconds)
            _drain_running_loop(
                bg_loop,
                timeout_seconds=device_flow_close_timeout_seconds,
            )
        except FutureTimeoutError as exc:
            try:
                _cancel_and_retrieve(
                    close_future,
                    completed,
                    timeout_seconds=device_flow_close_timeout_seconds,
                )
            except BaseException as cancel_exc:
                shutdown_error = cancel_exc
            else:
                shutdown_error = DeviceFlowCloseError(
                    "Feishu DeviceFlow close timed out after "
                    f"{device_flow_close_timeout_seconds:g} seconds"
                )
            if shutdown_error is not exc:
                shutdown_error.__cause__ = exc
        except BaseException as exc:
            shutdown_error = (
                exc
                if isinstance(exc, SdkBackgroundShutdownError)
                else DeviceFlowCloseError("Feishu DeviceFlow close failed")
            )
            if shutdown_error is not exc:
                shutdown_error.__cause__ = exc

    try:
        channel.stop()
    except BaseException as exc:
        if shutdown_error is None:
            shutdown_error = exc

    try:
        _verify_background_stopped(bg_loop, bg_thread)
    except BaseException as exc:
        if shutdown_error is None:
            shutdown_error = exc

    if shutdown_error is not None:
        raise shutdown_error
