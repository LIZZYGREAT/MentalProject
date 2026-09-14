"""Low-frequency physical cleanup for expired CardAction receipts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging


logger = logging.getLogger(__name__)


class CardActionReceiptMaintenanceScheduler:
    def __init__(
        self,
        repository,
        *,
        interval_seconds: int = 3600,
        batch_size: int = 500,
    ) -> None:
        self.repository = repository
        self.interval_seconds = max(60, int(interval_seconds))
        self.batch_size = max(1, min(int(batch_size), 5000))
        self._stop = asyncio.Event()
        self.started = asyncio.Event()

    async def run_once(self, now: datetime | None = None) -> int:
        instant = now or datetime.now(timezone.utc)
        return await asyncio.to_thread(
            self.repository.purge_expired,
            now=instant,
            limit=self.batch_size,
        )

    async def run_forever(self) -> None:
        self.started.set()
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("card_action_receipt_expiry_purge_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        self._stop.set()
