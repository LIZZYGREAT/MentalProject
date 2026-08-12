"""Production entry point: BotWorker -> ClaudeSDKClient -> DeepSeek -> MCP."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any


async def _run_gateway_until_shutdown(gateway: Any) -> None:
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    gateway_start = asyncio.create_task(gateway.start(), name="feishu-gateway-start")
    shutdown_requested = asyncio.create_task(
        shutdown.wait(), name="process-shutdown-wait"
    )
    tasks: set[asyncio.Task] = {gateway_start, shutdown_requested}
    try:
        done, _ = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_requested in done:
            gateway_start.cancel()
            await asyncio.gather(gateway_start, return_exceptions=True)
            return
        await gateway_start

        gateway_closed = asyncio.create_task(
            gateway.wait_closed(), name="feishu-gateway-wait"
        )
        tasks.add(gateway_closed)
        done, _ = await asyncio.wait(
            {gateway_closed, shutdown_requested},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if gateway_closed in done:
            await gateway_closed
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for sig in installed_signals:
            loop.remove_signal_handler(sig)


async def run() -> None:
    # Heavy application/algorithm imports are intentionally inside run().
    # Spawned Feishu receiver children importing app.main stay lightweight.
    from sqlalchemy import text
    from app.agent.claude_runtime import ClaudeAgentRuntime
    from app.agent.sdk_adapter import ProductionClaudeClientFactory, isolate_process_environment
    from app.agent.session_manager import ParticipantSessionManager
    from app.agent.skill_loader import SkillLoader
    from app.bootstrap import build_business_services
    from app.config import Settings
    from app.db import Database, build_engine
    from app.identity.service import IdentityService
    from app.integrations.feishu.client import FeishuClient
    from app.integrations.feishu.gateway import BotEvent, FeishuGateway
    from app.repositories import (
        AgentRunRepository, BindingRepository, BotEventRepository,
        ClaudeSessionRepository, ParticipantRepository,
    )
    from app.services.forecast_scheduler import ForecastScheduler
    from app.services.safety_service import SafetyService
    from app.worker import BotWorker

    settings = Settings.from_env()
    isolate_process_environment()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        import resource
        rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, AttributeError):
        rss_kib = 0
    logging.getLogger(__name__).info(
        "startup_resources pid=%s rss_kib=%s", os.getpid(), rss_kib
    )
    database = Database(build_engine(settings.database_url))
    with database.session() as session:
        session.execute(text("SELECT 1"))

    bindings = BindingRepository(database)
    identity = IdentityService(database, bindings)
    events = BotEventRepository(database)
    runs = AgentRunRepository(database)
    business = build_business_services(database, settings, runs)

    skill_loader = SkillLoader(settings.care_skill_path)
    skill_loader.load()
    factory = ProductionClaudeClientFactory(
        business.registry,
        workdir=settings.claude_workdir,
        plugin_path=settings.claude_plugin_path,
        settings_path=settings.claude_settings_path,
        model=settings.claude_model,
        opus_model=settings.claude_default_opus_model,
        sonnet_model=settings.claude_default_sonnet_model,
        haiku_model=settings.claude_default_haiku_model,
        subagent_model=settings.claude_code_subagent_model,
        base_url=settings.claude_anthropic_base_url,
        auth_token=settings.deepseek_api_key,
        max_turns=settings.claude_max_turns,
    )
    factory.validate()
    sessions = ParticipantSessionManager(
        factory,
        ClaudeSessionRepository(database),
        max_active_sessions=settings.max_active_agent_sessions,
        idle_timeout_seconds=settings.agent_session_idle_seconds,
        turn_timeout_seconds=settings.claude_timeout_seconds,
        input_queue_size=settings.participant_input_queue_size,
    )
    runtime = ClaudeAgentRuntime(
        sessions,
        business.conversations,
        SafetyService(),
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
        business.device_flows,
        model=f"claude-code/{settings.claude_model}",
        max_retries=settings.feishu_send_max_retries,
        progress_delay_seconds=settings.progress_delay_seconds,
        progress_cooldown_seconds=settings.progress_cooldown_seconds,
        progress_max_messages=settings.progress_max_messages,
    )
    gateway = FeishuGateway(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        identity,
        events,
        queue,
        start_timeout_seconds=settings.feishu_gateway_start_timeout_seconds,
        stop_timeout_seconds=settings.feishu_gateway_stop_timeout_seconds,
        device_flow_close_timeout_seconds=(
            settings.feishu_gateway_device_flow_close_timeout_seconds
        ),
    )
    scheduler = ForecastScheduler(
        coordinator=business.forecast_coordinator,
        participants=ParticipantRepository(database),
        warnings=business.warning_schedules,
        bindings=bindings, sender=sender,
        timezone_name=settings.timezone_name,
        daily_prepare_local_time=settings.forecast_daily_prepare_local_time,
        calendar_sync_interval_seconds=settings.forecast_calendar_sync_interval_seconds,
        warning_poll_interval_seconds=settings.warning_poll_interval_seconds,
    )
    # Start the consumer before recovery.  Queue capacity can be smaller than
    # the durable backlog without causing startup deadlock.
    dispatcher = asyncio.create_task(worker.run_forever(), name="bot-dispatcher")
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
    for participant_id in business.device_flows.pending_participants():
        worker.resume_device_flow(participant_id)

    forecast_tasks = asyncio.create_task(scheduler.run_forever(), name="forecast-scheduler")
    try:
        await _run_gateway_until_shutdown(gateway)
    finally:
        try:
            await gateway.stop()
        finally:
            await scheduler.close()
            forecast_tasks.cancel()
            dispatcher.cancel()
            await asyncio.gather(dispatcher, forecast_tasks, return_exceptions=True)
            try:
                await worker.close()
            finally:
                await runtime.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
