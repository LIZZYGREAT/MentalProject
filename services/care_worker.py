"""Persistent Feishu inbox worker with finite retries and crash recovery."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from auth.database import AppDatabase
from integrations.feishu.client import FeishuBotClient, FeishuSendError
from services.care_delivery import CareDeliveryPolicy
from services.feishu_message_processor import FeishuMessageProcessor
from services.proactive_care import ProactiveCareScheduler


logger = logging.getLogger(__name__)


class CareWorker:
    def __init__(
        self,
        database: AppDatabase,
        processor: FeishuMessageProcessor,
        bot_client: FeishuBotClient,
        *,
        worker_id: str,
        poll_seconds: float = 1.0,
        max_attempts: int = 5,
        lease_seconds: int = 120,
        proactive_scheduler: Optional[ProactiveCareScheduler] = None,
        proactive_interval_seconds: float = 60.0,
    ):
        self.database = database
        self.processor = processor
        self.bot_client = bot_client
        self.worker_id = str(worker_id)
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.lease_seconds = max(5, int(lease_seconds))
        self.deliveries = CareDeliveryPolicy(database)
        self.proactive_scheduler = proactive_scheduler
        self.proactive_interval_seconds = max(15.0, float(proactive_interval_seconds))
        self._next_proactive_check = 0.0

    def run_once(self) -> bool:
        self.database.update_bot_heartbeat(
            "care_worker",
            "online",
            {"worker_id": self.worker_id, "max_attempts": self.max_attempts},
        )
        event = self.database.claim_feishu_event(
            self.worker_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if event is None:
            return self._run_proactive_once()
        try:
            outbound = self.processor.process(event)
            if outbound.ignored:
                self.database.complete_feishu_event(event["event_id"], ignored=True)
                return True
            if outbound.msg_type == "text":
                provider_message_id = self.bot_client.send_text(
                    outbound.chat_id,
                    str(outbound.content.get("text") or ""),
                )
            else:
                provider_message_id = self.bot_client.send_card(
                    outbound.chat_id,
                    outbound.content,
                )
            if outbound.delivery_id:
                self.deliveries.mark_sent(outbound.delivery_id, provider_message_id)
            self.database.complete_feishu_event(event["event_id"])
            return True

        except FeishuSendError as exc:
            delay = min(60, 2 ** max(0, int(event["attempts"]) - 1))
            status = self.database.retry_feishu_event(
                event["event_id"],
                f"send_error code={exc.code}",
                max_attempts=self.max_attempts,
                delay_seconds=delay,
                retryable=exc.retryable,
            )
            if status in {"failed", "dead_letter"}:
                outbound_delivery = locals().get("outbound")
                if outbound_delivery and outbound_delivery.delivery_id:
                    self.deliveries.mark_failed(
                        outbound_delivery.delivery_id,
                        f"send_error code={exc.code}",
                    )
            return True
        except ValueError as exc:
            self.database.retry_feishu_event(
                event["event_id"],
                str(exc),
                max_attempts=self.max_attempts,
                delay_seconds=0,
                retryable=False,
            )
            return True
        except Exception as exc:
            logger.exception("Feishu event processing failed event_id=%s", event["event_id"])
            delay = min(60, 2 ** max(0, int(event["attempts"]) - 1))
            self.database.retry_feishu_event(
                event["event_id"],
                type(exc).__name__,
                max_attempts=self.max_attempts,
                delay_seconds=delay,
                retryable=True,
            )
            return True

    def _run_proactive_once(self) -> bool:
        if self.proactive_scheduler is None or time.monotonic() < self._next_proactive_check:
            return False
        self._next_proactive_check = time.monotonic() + self.proactive_interval_seconds
        candidate = self.proactive_scheduler.next_candidate()
        if candidate is None:
            return False
        try:
            provider_message_id = self.bot_client.send_card(
                candidate.chat_id,
                candidate.content,
            )
            self.deliveries.mark_sent(candidate.delivery_id, provider_message_id)
        except FeishuSendError as exc:
            self.deliveries.mark_failed(
                candidate.delivery_id,
                f"send_error code={exc.code}",
            )
        except Exception as exc:
            logger.exception("Proactive Feishu delivery failed kind=%s", candidate.kind)
            self.deliveries.mark_failed(candidate.delivery_id, type(exc).__name__)
        return True

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        stop_event = stop_event or threading.Event()
        try:
            while not stop_event.is_set():
                processed = self.run_once()
                if not processed:
                    stop_event.wait(self.poll_seconds)
        finally:
            self.database.update_bot_heartbeat(
                "care_worker",
                "offline",
                {"worker_id": self.worker_id},
            )
