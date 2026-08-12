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


def stop_feishu_channel_cleanly(
    channel: Any,
    *,
    device_flow_close_timeout_seconds: float,
    sdk_version: str | None = None,
) -> None:
    """Pre-close DeviceFlow on the SDK loop, then invoke the public stop API."""

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
    device_flow = getattr(channel, "_device_flow", None)
    close = getattr(device_flow, "close", None)
    if (
        bg_loop is None
        or not callable(getattr(bg_loop, "is_running", None))
        or not bg_loop.is_running()
        or not callable(close)
    ):
        raise DeviceFlowCloseError(
            "lark-channel-sdk 1.2.0 shutdown internals are unavailable"
        )

    completed = threading.Event()

    async def close_device_flow() -> None:
        try:
            await close()
        finally:
            completed.set()

    close_coroutine = close_device_flow()
    try:
        close_future = asyncio.run_coroutine_threadsafe(close_coroutine, bg_loop)
    except BaseException:
        close_coroutine.close()
        raise
    try:
        close_future.result(timeout=device_flow_close_timeout_seconds)
    except FutureTimeoutError as exc:
        _cancel_and_retrieve(
            close_future,
            completed,
            timeout_seconds=device_flow_close_timeout_seconds,
        )
        raise DeviceFlowCloseError(
            "Feishu DeviceFlow close timed out after "
            f"{device_flow_close_timeout_seconds:g} seconds"
        ) from exc
    except BaseException as exc:
        raise DeviceFlowCloseError("Feishu DeviceFlow close failed") from exc

    channel.stop()
