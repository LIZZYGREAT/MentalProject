"""Bounded, in-memory download and validation of inbound message images."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any, Callable


class MessageResourceError(RuntimeError):
    pass


class MessageResourceTooLarge(MessageResourceError):
    pass


class UnsupportedImageFormat(MessageResourceError):
    pass


@dataclass(frozen=True)
class DownloadedImage:
    data: bytes
    mime_type: str


class FeishuMessageResourceDownloader:
    """Use the bot app's official message-resource API, never Calendar OAuth."""

    def __init__(
        self,
        client: Any,
        *,
        max_bytes: int = 8 * 1024 * 1024,
        timeout_seconds: float = 12.0,
        max_concurrency: int = 1,
        download: Callable[[str, str], bytes] | None = None,
    ) -> None:
        self.client = client
        self.max_bytes = int(max_bytes)
        self.timeout_seconds = float(timeout_seconds)
        self.download = download or client.download_message_image
        self._download_slots = asyncio.Semaphore(max(1, int(max_concurrency)))
        if self.max_bytes < 1 or self.timeout_seconds <= 0:
            raise ValueError("message resource limits must be positive")

    async def download_image(self, message_id: str, image_key: str) -> DownloadedImage:
        await self._download_slots.acquire()
        task = asyncio.create_task(
            asyncio.to_thread(
                self._download_bounded, str(message_id), str(image_key)
            )
        )
        task.add_done_callback(self._release_download_slot)
        try:
            raw = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MessageResourceError("message image download timed out") from exc
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise MessageResourceError("message image download returned no data")
        data = bytes(raw)
        if len(data) > self.max_bytes:
            raise MessageResourceTooLarge("message image exceeds configured limit")
        mime_type = detect_image_mime(data)
        if mime_type is None:
            raise UnsupportedImageFormat("only JPEG, PNG, and WebP images are supported")
        return DownloadedImage(data=data, mime_type=mime_type)

    def _release_download_slot(self, task: asyncio.Task[bytes]) -> None:
        self._download_slots.release()
        if task.cancelled():
            return
        # Consume an exception when the caller already timed out or was
        # cancelled so the detached to_thread task cannot emit a warning.
        task.exception()

    def _download_bounded(self, message_id: str, image_key: str) -> bytes:
        try:
            parameters = inspect.signature(self.download).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "max_bytes" in parameters:
            return self.download(message_id, image_key, max_bytes=self.max_bytes)
        return self.download(message_id, image_key)


def detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
