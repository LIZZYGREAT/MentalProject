"""Production entry point: BotWorker -> ClaudeSDKClient -> DeepSeek -> MCP."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any


def _log_startup_phase(name: str) -> None:
    try:
        import resource
        rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, AttributeError):
        rss_kib = 0
    logging.getLogger(__name__).info(
        "startup_phase=%s pid=%s rss_kib=%s", name, os.getpid(), rss_kib
    )


def _build_bot_transport(
    settings: Any,
    identity: Any,
    events: Any,
    queue: Any,
    *,
    client_factory: Any = None,
    gateway_factory: Any = None,
) -> tuple[Any, Any]:
    if client_factory is None:
        from app.integrations.feishu.client import FeishuClient

        client_factory = FeishuClient
    if gateway_factory is None:
        from app.integrations.feishu.gateway import FeishuGateway

        gateway_factory = FeishuGateway
    sender = client_factory(
        settings.feishu_bot_app_id, settings.feishu_bot_app_secret
    )
    gateway = gateway_factory(
        settings.feishu_bot_app_id,
        settings.feishu_bot_app_secret,
        identity,
        events,
        queue,
        start_timeout_seconds=settings.feishu_gateway_start_timeout_seconds,
        stop_timeout_seconds=settings.feishu_gateway_stop_timeout_seconds,
        device_flow_close_timeout_seconds=(
            settings.feishu_gateway_device_flow_close_timeout_seconds
        ),
    )
    return sender, gateway


async def _run_gateway_until_shutdown(gateway: Any, on_ready: Any = None) -> None:
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
        if on_ready is not None:
            await on_ready()

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
    from app.integrations.feishu.gateway import BotEvent
    from app.repositories import (
        AgentRunRepository, BindingRepository, BotEventRepository,
        ClaudeSessionRepository, ParticipantRepository,
    )
    from app.services.forecast_scheduler import ForecastScheduler
    from app.services.safety_service import SafetyService
    from app.worker import BotWorker
    from app.logging_security import install_credential_redaction

    settings = Settings.from_env()
    isolate_process_environment()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    install_credential_redaction()
    _log_startup_phase("settings_ready")
    database = Database(build_engine(settings.database_url))
    with database.session() as session:
        session.execute(text("SELECT 1"))
    _log_startup_phase("database_ready")

    bindings = BindingRepository(database)
    identity = IdentityService(database, bindings)
    events = BotEventRepository(database)
    runs = AgentRunRepository(database)
    business = build_business_services(database, settings, runs)
    _log_startup_phase("business_ready")

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
    sender, gateway = _build_bot_transport(settings, identity, events, queue)
    worker = BotWorker(
        queue,
        identity,
        events,
        runs,
        skill_loader,
        runtime,
        sender,
        business.device_flows,
        business.presentations,
        model=f"claude-code/{settings.claude_model}",
        max_retries=settings.feishu_send_max_retries,
        progress_delay_seconds=settings.progress_delay_seconds,
        progress_cooldown_seconds=settings.progress_cooldown_seconds,
        progress_max_messages=settings.progress_max_messages,
    )
    scheduler = ForecastScheduler(
        coordinator=business.forecast_coordinator,
        participants=ParticipantRepository(database),
        warnings=business.warning_schedules,
        bindings=bindings, sender=sender,
        timezone_name=settings.timezone_name,
        calendar_oauth_app_id=settings.feishu_calendar_app_id,
        daily_prepare_local_time=settings.forecast_daily_prepare_local_time,
        calendar_sync_interval_seconds=settings.forecast_calendar_sync_interval_seconds,
        warning_poll_interval_seconds=settings.warning_poll_interval_seconds,
        forecast_max_concurrency=settings.forecast_max_concurrency,
        warning_max_attempts=settings.warning_max_attempts,
        warning_retry_base_seconds=settings.warning_retry_base_seconds,
        warning_claim_lease_seconds=settings.warning_claim_lease_seconds,
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

    forecast_tasks: asyncio.Task | None = None

    async def start_scheduler_after_gateway_ready() -> None:
        nonlocal forecast_tasks
        _log_startup_phase("gateway_ready")
        forecast_tasks = asyncio.create_task(
            scheduler.run_forever(), name="forecast-scheduler"
        )
        await scheduler.started.wait()
        _log_startup_phase("forecast_scheduler_ready")

    try:
        # Gateway readiness is a hard startup gate. Forecast work cannot
        # compete with receiver spawn/connection on the small ECS host.
        await _run_gateway_until_shutdown(
            gateway, on_ready=start_scheduler_after_gateway_ready
        )
    finally:
        try:
            await gateway.stop()
        finally:
            await scheduler.close()
            if forecast_tasks is not None:
                forecast_tasks.cancel()
            dispatcher.cancel()
            await asyncio.gather(
                dispatcher,
                *(task for task in (forecast_tasks,) if task is not None),
                return_exceptions=True,
            )
            try:
                await worker.close()
            finally:
                try:
                    await business.semantic_preprocessor.close(
                        settings.semantic_api_timeout_seconds + 2
                    )
                finally:
                    await runtime.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
