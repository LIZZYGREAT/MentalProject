"""Standalone persistent care worker process."""

from __future__ import annotations

import os
import socket

from auth.database import AppDatabase
from integrations.feishu.client import FeishuBotClient
from integrations.feishu.identity import FeishuIdentityService
from services.care_service import CareService
from services.care_worker import CareWorker
from services.feishu_message_processor import FeishuMessageProcessor
from services.proactive_care import ProactiveCareScheduler


def build_worker() -> CareWorker:
    database = AppDatabase()
    database.init_schema()
    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    base_url = os.getenv("FEISHU_BIND_BASE_URL", "")
    identity = FeishuIdentityService(
        database,
        app_id=app_id,
        bind_base_url=base_url,
        token_ttl_seconds=int(os.getenv("FEISHU_BIND_TOKEN_TTL_SECONDS", "900")),
    )
    care_service = CareService(database)
    proactive_scheduler = ProactiveCareScheduler(
        database,
        care_service,
        lead_minutes=int(os.getenv("CARE_PROACTIVE_LEAD_MINUTES", "90")),
    )
    processor = FeishuMessageProcessor(
        database,
        identity,
        care_service,
        web_base_url=base_url,
        private_chat_only=os.getenv("FEISHU_BOT_PRIVATE_CHAT_ONLY", "true").lower()
        == "true",
    )
    return CareWorker(
        database,
        processor,
        FeishuBotClient(app_id, app_secret),
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
        poll_seconds=float(os.getenv("CARE_WORKER_POLL_SECONDS", "1")),
        max_attempts=int(os.getenv("CARE_WORKER_MAX_ATTEMPTS", "5")),
        lease_seconds=int(os.getenv("CARE_EVENT_LEASE_SECONDS", "120")),
        proactive_scheduler=proactive_scheduler,
        proactive_interval_seconds=float(
            os.getenv("CARE_PROACTIVE_INTERVAL_SECONDS", "60")
        ),
    )


def main() -> None:
    if (
        os.getenv("CARE_WORKER_ENABLED", "true").lower() != "true"
        or os.getenv("FEISHU_BOT_ENABLED", "false").lower() != "true"
    ):
        return
    build_worker().run_forever()


if __name__ == "__main__":
    main()
