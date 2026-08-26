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


def _should_start_daily_review_scheduler(
    settings: Any, card_callback: Any
) -> bool:
    """Fail closed if an interactive Daily Review card cannot submit."""

    if not bool(settings.daily_review_enabled):
        return False
    if card_callback is None:
        logging.getLogger(__name__).error(
            "daily_review_scheduler_disabled callback_unavailable"
        )
        return False
    return True


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
    from app.integrations.feishu.card_callback import FeishuCardCallbackServer
    from app.repositories import (
        AgentRunRepository, BindingRepository, BotEventRepository,
        ClaudeSessionRepository, ParticipantRepository, RuntimeIncidentRepository,
    )
    from app.services.forecast_scheduler import ForecastScheduler
    from app.services.daily_review_scheduler import DailyReviewScheduler
    from app.services.safety_service import SafetyService
    from app.presentation.presentation_agent import ProductionPresentationAgent
    from app.presentation.progress_presenter import ProgressPresenter
    from app.presentation.response_orchestrator import ResponseOrchestrator
    from app.presentation.semantic_segmenter import SemanticSegmenter
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
    incidents = RuntimeIncidentRepository(database)
    runs = AgentRunRepository(database)
    business = build_business_services(database, settings, runs)
    business.dependency_refresh.start()
    business.observation_refresh.start()
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
        partial_messages_enabled=settings.claude_partial_messages_enabled,
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
    presentation_agent = (
        ProductionPresentationAgent(
            workdir=settings.claude_workdir,
            model=settings.presentation_model,
            base_url=settings.claude_anthropic_base_url,
            auth_token=settings.deepseek_api_key,
            opus_model=settings.claude_default_opus_model,
            sonnet_model=settings.claude_default_sonnet_model,
            haiku_model=settings.claude_default_haiku_model,
            disconnect_timeout_seconds=(
                settings.presentation_agent_disconnect_timeout_seconds
            ),
        )
        if settings.presentation_agent_mode != "off"
        else None
    )
    response_orchestrator = ResponseOrchestrator(
        segmenter=SemanticSegmenter(
            enabled=settings.response_segmentation_enabled,
            min_total_chars=settings.response_segment_min_total_chars,
            target_chars=settings.response_segment_target_chars,
            max_chars=settings.response_segment_max_chars,
            max_segments=settings.response_max_segments,
        ),
        presentation_agent=presentation_agent,
        presentation_agent_mode=settings.presentation_agent_mode,
        presentation_agent_min_chars=settings.presentation_agent_min_chars,
        presentation_agent_timeout_seconds=(
            settings.presentation_agent_timeout_seconds
        ),
        presentation_agent_max_pending_cleanups=(
            settings.presentation_agent_max_pending_cleanups
        ),
        presentation_agent_max_segments=(
            settings.presentation_agent_max_segments
        ),
    )

    queue: asyncio.Queue[BotEvent] = asyncio.Queue(
        maxsize=settings.queue_max_size
    )
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
        progress_presenter=ProgressPresenter(),
        response_orchestrator=response_orchestrator,
        max_retries=settings.feishu_send_max_retries,
        progress_delay_seconds=settings.progress_delay_seconds,
        progress_cooldown_seconds=settings.progress_cooldown_seconds,
        progress_max_messages=settings.progress_max_messages,
        incidents=incidents,
    )
    def handle_card_action(event: Any) -> dict[str, Any]:
        participant = identity.resolve(event.app_id, event.open_id)
        if participant is None:
            raise ValueError("card operator is not bound to a participant")
        return business.card_actions.handle(
            participant.id,
            message_id=event.message_id,
            callback_event_id=event.event_id,
            action_value=event.action_value,
            form_value=event.form_value,
        )

    card_callback = (
        FeishuCardCallbackServer(
            app_id=settings.feishu_bot_app_id,
            verification_token=settings.feishu_card_verification_token,
            encrypt_key=settings.feishu_card_encrypt_key,
            action_handler=handle_card_action,
            host=settings.feishu_card_callback_host,
            port=settings.feishu_card_callback_port,
            path=settings.feishu_card_callback_path,
        )
        if settings.feishu_card_callback_enabled
        else None
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
        warning_delivery_policy=business.warning_schedules.delivery_policy,
        profile_calibration=(
            business.profile_calibration
            if settings.profile_calibration_enabled else None
        ),
        incidents=incidents,
        care_card_enabled=bool(card_callback is not None),
    )
    daily_review_scheduler = DailyReviewScheduler(
        schedules=business.daily_review_schedules,
        participants=ParticipantRepository(database),
        bindings=bindings,
        forecasts=business.forecast_snapshots,
        sender=sender,
        timezone_name=settings.daily_review_timezone,
        local_time=settings.daily_review_local_time,
        poll_interval_seconds=settings.daily_review_poll_interval_seconds,
        retry_base_seconds=settings.daily_review_retry_base_seconds,
        max_attempts=settings.daily_review_max_attempts,
        claim_lease_seconds=settings.daily_review_claim_lease_seconds,
        validity_minutes=settings.daily_review_validity_minutes,
        catch_up_minutes=settings.daily_review_catch_up_minutes,
        care_preferences=business.care_preferences,
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
    daily_review_tasks: asyncio.Task | None = None

    async def start_scheduler_after_gateway_ready() -> None:
        nonlocal forecast_tasks, daily_review_tasks
        _log_startup_phase("gateway_ready")
        forecast_tasks = asyncio.create_task(
            scheduler.run_forever(), name="forecast-scheduler"
        )
        if _should_start_daily_review_scheduler(settings, card_callback):
            daily_review_tasks = asyncio.create_task(
                daily_review_scheduler.run_forever(), name="daily-review-scheduler"
            )
        await scheduler.started.wait()
        if daily_review_tasks is not None:
            await daily_review_scheduler.started.wait()
        _log_startup_phase("forecast_scheduler_ready")

    try:
        if card_callback is not None:
            await card_callback.start()
            _log_startup_phase("card_callback_ready")
        # Gateway readiness is a hard startup gate. Forecast work cannot
        # compete with receiver spawn/connection on the small ECS host.
        await _run_gateway_until_shutdown(
            gateway, on_ready=start_scheduler_after_gateway_ready
        )
    finally:
        try:
            await gateway.stop()
        finally:
            if card_callback is not None:
                await card_callback.stop()
            await scheduler.close()
            await daily_review_scheduler.close()
            if forecast_tasks is not None:
                forecast_tasks.cancel()
            if daily_review_tasks is not None:
                daily_review_tasks.cancel()
            dispatcher.cancel()
            await asyncio.gather(
                dispatcher,
                *(task for task in (forecast_tasks, daily_review_tasks) if task is not None),
                return_exceptions=True,
            )
            try:
                await worker.close()
            finally:
                try:
                    await business.observation_refresh.close()
                finally:
                    try:
                        await business.dependency_refresh.close()
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
