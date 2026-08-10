"""Production entry point: python -m app.main."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.agent.runtime import AgentRuntime
from app.agent.skill_loader import SkillLoader
from app.agent.tool_registry import ToolRegistry
from app.config import Settings
from app.db import Database, build_engine
from app.identity.service import IdentityService
from app.integrations.deepseek import DeepSeekClient
from app.integrations.feishu.calendar import CalendarService
from app.integrations.feishu.client import FeishuClient
from app.integrations.feishu.gateway import BotEvent, FeishuGateway
from app.integrations.feishu.oauth import DeviceFlowService, FeishuOAuthClient
from app.repositories import (
    AgentRunRepository,
    BindingRepository,
    BotEventRepository,
    ConversationRepository,
    ObservationRepository,
    PredictionRepository,
    ProfileRepository,
)
from app.services.prediction_service import PredictionService
from app.services.safety_service import SafetyService
from app.services.token_service import (
    TokenEncryptionService,
    TokenRefreshService,
    TokenRepository,
)
from app.tools.care import CareTools
from app.worker import BotWorker
from mindflow_core.assessment import AssessmentModel


async def run() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = Database(build_engine(settings.database_url))
    with database.session() as session:
        session.execute(text("SELECT 1"))

    bindings = BindingRepository(database)
    identity = IdentityService(database, bindings)
    events = BotEventRepository(database)
    runs = AgentRunRepository(database)
    profiles = ProfileRepository(database)
    observations = ObservationRepository(database)
    predictions = PredictionRepository(database)
    conversations = ConversationRepository(database)

    encryption = TokenEncryptionService(settings.token_encryption_key)
    token_repo = TokenRepository(database, encryption)
    oauth = FeishuOAuthClient(settings.feishu_app_id, settings.feishu_app_secret)
    device_flows = DeviceFlowService(database, encryption, token_repo, oauth)
    refresh = TokenRefreshService(database, encryption, oauth.refresh_token)
    calendar = CalendarService(refresh)
    prediction_service = PredictionService(AssessmentModel(), predictions)

    skill_loader = SkillLoader(settings.care_skill_path)
    skill_loader.load()
    registry = ToolRegistry(runs)
    CareTools(
        profiles,
        observations,
        predictions,
        prediction_service,
        calendar,
        token_repo,
        settings.timezone_name,
    ).register(registry)
    deepseek = DeepSeekClient(
        settings.deepseek_api_key,
        settings.deepseek_base_url,
        settings.deepseek_model,
        timeout_seconds=settings.agent_timeout_seconds,
    )
    runtime = AgentRuntime(
        deepseek,
        skill_loader,
        registry,
        conversations,
        SafetyService(),
        history_limit=settings.history_limit,
        max_tool_steps=settings.max_tool_steps,
        timeout_seconds=settings.agent_timeout_seconds,
        max_retries=settings.max_retries,
    )
    queue: asyncio.Queue[BotEvent] = asyncio.Queue(maxsize=settings.queue_max_size)
    sender = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    worker = BotWorker(
        queue,
        identity,
        events,
        runs,
        skill_loader,
        runtime,
        sender,
        device_flows,
        model=settings.deepseek_model,
        max_retries=settings.max_retries,
    )
    gateway = FeishuGateway(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        identity,
        events,
        queue,
    )
    workers = [
        asyncio.create_task(worker.run_forever(), name=f"agent-worker-{index}")
        for index in range(settings.agent_workers)
    ]
    for saved in events.recoverable():
        await queue.put(
            BotEvent(
                event_id=saved.event_id,
                message_id=saved.message_id,
                app_id=saved.app_id,
                open_id=saved.open_id,
                chat_id=saved.chat_id,
                text=saved.text,
                create_time=saved.create_time,
                chat_type=saved.chat_type,
            )
        )
    for participant_id in device_flows.pending_participants():
        worker.resume_device_flow(participant_id)
    try:
        await gateway.start()
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
