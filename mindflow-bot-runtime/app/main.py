"""Production entry point: BotWorker -> ClaudeSDKClient -> DeepSeek -> MCP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import time
import uuid
from datetime import datetime, timezone
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


def _log_web_search_config(settings: Any) -> None:
    logging.getLogger(__name__).info(
        "web_search_config enabled=%s provider=%s model=%s key_configured=%s",
        bool(settings.web_search_enabled),
        str(settings.web_search_provider),
        str(settings.web_search_model),
        bool(str(settings.deepseek_api_key or "").strip()),
    )


def _record_streaming_capability(settings: Any, sender: Any, incidents: Any) -> None:
    enabled = bool(settings.feishu_streaming_card_enabled)
    sender_supported = callable(getattr(sender, "start_streaming_card", None))
    logging.getLogger(__name__).info(
        "feishu_streaming_capability enabled=%s sender_supported=%s",
        enabled,
        sender_supported,
    )
    if not enabled or sender_supported:
        return
    logging.getLogger(__name__).warning(
        "feishu_streaming_capability_unavailable enabled=true sender_supported=false"
    )
    incidents.record(
        severity="warning",
        subsystem="feishu",
        event_name="feishu_streaming_capability_unavailable",
        summary="Streaming is enabled but the configured sender has no CardKit capability.",
        details={"enabled": True, "sender_supported": False},
    )


def _should_start_daily_review_scheduler(
    settings: Any, card_action_transport_available: bool
) -> bool:
    """Fail closed if an interactive Daily Review card cannot submit."""

    if not bool(settings.daily_review_enabled):
        return False
    if not card_action_transport_available:
        logging.getLogger(__name__).error(
            "daily_review_scheduler_disabled card_action_transport_unavailable"
        )
        return False
    return True


def _build_bot_transport(
    settings: Any,
    identity: Any,
    events: Any,
    queue: Any,
    *,
    sender: Any = None,
    card_action_handler: Any = None,
    client_factory: Any = None,
    gateway_factory: Any = None,
) -> tuple[Any, Any]:
    if client_factory is None:
        from app.integrations.feishu.client import FeishuClient

        client_factory = FeishuClient
    if gateway_factory is None:
        from app.integrations.feishu.gateway import FeishuGateway

        gateway_factory = FeishuGateway
    if sender is None:
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
        card_action_handler=(
            card_action_handler
            if settings.feishu_card_action_transport == "ws"
            else None
        ),
    )
    return sender, gateway


def _card_action_transport_available(settings: Any, card_callback: Any) -> bool:
    return bool(
        settings.feishu_card_action_transport == "ws"
        or (
            settings.feishu_card_action_transport == "http"
            and card_callback is not None
        )
    )


def _build_card_callback(
    settings: Any, action_handler: Any, *, server_factory: Any = None
) -> Any:
    if (
        settings.feishu_card_action_transport != "http"
        or not settings.feishu_card_callback_enabled
    ):
        return None
    if server_factory is None:
        from app.integrations.feishu.card_callback import FeishuCardCallbackServer

        server_factory = FeishuCardCallbackServer
    return server_factory(
        app_id=settings.feishu_bot_app_id,
        verification_token=settings.feishu_card_verification_token,
        encrypt_key=settings.feishu_card_encrypt_key,
        action_handler=action_handler,
        host=settings.feishu_card_callback_host,
        port=settings.feishu_card_callback_port,
        path=settings.feishu_card_callback_path,
    )


def _build_card_action_handler(
    identity: Any,
    card_actions: Any,
    sender: Any = None,
    incidents: Any = None,
    receipts: Any = None,
    backend_state_events: Any = None,
) -> Any:
    """Compatibility constructor composed from executor and WS delivery."""

    if sender is not None:
        from app.card_actions.delivery import build_card_action_delivery

        executor = _build_card_action_handler(
            identity, card_actions, None, incidents, receipts, backend_state_events
        )
        return build_card_action_delivery(executor, sender, incidents)

    from app.integrations.feishu.cards import card_action_result_card
    from app.card_actions.delivery import card_update_failure_policy
    from app.card_actions.registry import card_action_spec

    def safe_action_name(event: Any) -> str | None:
        candidate = str((event.action_value or {}).get("mindflow_action") or "")
        return candidate if card_action_spec(candidate) is not None else None

    def replacement_message_uuid(event_id: str) -> str:
        digest = hashlib.sha256(
            f"mindflow:card-action-result:{event_id}".encode("utf-8")
        ).hexdigest()
        return f"mindflow-{digest[:40]}"

    def receipt_identity(event: Any) -> tuple[str, str, str, str]:
        action_name = safe_action_name(event) or "unknown"
        action_version = str(
            (event.action_value or {}).get("version") or "1"
        )[:16]
        canonical = json.dumps(
            {
                "message_id": event.message_id,
                "action_tag": event.action_tag,
                "action_value": event.action_value,
                "form_value": event.form_value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return (
            action_name,
            action_version,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            hashlib.sha256(str(event.message_id).encode("utf-8")).hexdigest(),
        )

    def log_stage(
        event: Any,
        stage: str,
        *,
        participant_id: Any = None,
        result_ok: bool | None = None,
        navigation_only: bool = False,
        card_update_ok: bool | None = None,
        provider_error_code: Any = None,
        error_class: str | None = None,
        error_id: str | None = None,
        business_latency_ms: float | None = None,
        card_update_latency_ms: float | None = None,
        total_card_action_latency_ms: float | None = None,
        exc_info: bool = False,
    ) -> None:
        logging.getLogger(__name__).log(
            logging.ERROR if error_class else logging.INFO,
            (
                "%s event_id=%s message_id=%s action_name=%s "
                "participant_id=%s result_ok=%s navigation_only=%s "
                "card_update_ok=%s provider_error_code=%s error_class=%s "
                "error_id=%s business_latency_ms=%s card_update_latency_ms=%s "
                "total_card_action_latency_ms=%s"
            ),
            stage,
            event.event_id,
            event.message_id,
            safe_action_name(event),
            participant_id,
            result_ok,
            navigation_only,
            card_update_ok,
            provider_error_code,
            error_class,
            error_id,
            business_latency_ms,
            card_update_latency_ms,
            total_card_action_latency_ms,
            exc_info=exc_info,
        )

    def record_failure(
        event: Any,
        *,
        event_name: str = "card_action_failed",
        summary: str,
        error_code: str | None,
        error_class: str | None,
        participant_id: Any = None,
        severity: str = "error",
        error_id: str | None = None,
        provider_error_code: Any = None,
        exc_info: bool = False,
    ) -> None:
        log_stage(
            event,
            event_name,
            participant_id=participant_id,
            result_ok=False,
            provider_error_code=provider_error_code,
            error_class=error_class,
            error_id=error_id,
            exc_info=exc_info,
        )
        if incidents is not None:
            try:
                incidents.record(
                    severity=severity,
                    subsystem="feishu_card_action",
                    event_name=event_name,
                    summary=summary,
                    participant_id=participant_id,
                    bot_event_id=event.event_id,
                    error_code=error_code,
                    error_class=error_class,
                    details={
                        "message_id": event.message_id,
                        "action_tag": event.action_tag,
                        "action_name": safe_action_name(event),
                        "error_id": error_id,
                        "provider_error_code": provider_error_code,
                    },
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "feishu_card_action_incident_record_failed "
                    "event_id=%s message_id=%s incident_event=%s",
                    event.event_id,
                    event.message_id,
                    event_name,
                )

    def notify_failure(event: Any) -> None:
        if sender is None:
            return
        try:
            sender.send_text(event.chat_id, "操作未能完成，请稍后重试。")
        except Exception:
            logging.getLogger(__name__).exception(
                "feishu_card_action_failure_notice_failed "
                "event_id=%s message_id=%s",
                event.event_id,
                event.message_id,
            )

    def notify_card_update_failure(event: Any, *, navigation_only: bool = False) -> None:
        if sender is None:
            return
        message = (
            "功能卡暂时没能更新，重新发“功能”即可。"
            if navigation_only
            else "操作已经完成，但卡片状态暂未更新，无需重复点击。"
        )
        try:
            sender.send_text(event.chat_id, message)
        except Exception:
            logging.getLogger(__name__).exception(
                "feishu_card_action_update_notice_failed "
                "event_id=%s message_id=%s",
                event.event_id,
                event.message_id,
            )

    def handle_card_action(event: Any) -> dict[str, Any]:
        total_started = time.monotonic()
        business_started = total_started
        participant = None
        receipt_fingerprint = None
        receipt_claimed = False
        log_stage(event, "card_action_received")
        try:
            participant = identity.resolve(event.app_id, event.open_id)
            if participant is None:
                raise ValueError("card operator is not bound to a participant")
            action_spec = card_action_spec(safe_action_name(event))
            action_version = str(
                (event.action_value or {}).get("version") or ""
            ).strip()
            receipt_required = bool(
                receipts is not None
                and action_spec is not None
                and action_version in action_spec.versions
                and action_spec.replay_policy == "receipt_required"
            )
            if receipt_required:
                (
                    action_name,
                    action_version,
                    receipt_fingerprint,
                    message_id_hash,
                ) = receipt_identity(event)
                claim = receipts.claim(
                    event_id=event.event_id,
                    participant_id=participant.id,
                    action_name=action_name,
                    action_version=action_version,
                    action_fingerprint=receipt_fingerprint,
                    message_id_hash=message_id_hash,
                )
                if claim.outcome == "conflict":
                    raise PermissionError("CardAction event identity conflict")
                receipt_claimed = claim.outcome == "claimed"
                if claim.outcome == "replay":
                    log_stage(
                        event,
                        "card_action_duplicate_replayed",
                        participant_id=participant.id,
                        result_ok=claim.status == "succeeded",
                    )
                    if claim.status == "processing":
                        return {
                            "ok": False,
                            "error": "card_action_processing",
                            "reply_text": "操作正在处理中，请勿重复点击。",
                            "receipt_replayed": True,
                        }
                    if claim.status == "succeeded":
                        reply_text = "该操作已经处理，无需重复点击。"
                        return {
                            "ok": True,
                            "receipt_replayed": True,
                            "reply_text": reply_text,
                            "card": card_action_result_card(message=reply_text),
                        }
                    return {
                        "ok": False,
                        "error": claim.error_code or "card_action_failed",
                        "receipt_replayed": True,
                        "reply_text": (
                            "该操作此前未完成，请重新打开最新卡片后重试。"
                        ),
                    }
                else:
                    result = card_actions.handle(
                        participant.id,
                        message_id=event.message_id,
                        chat_id=event.chat_id,
                        callback_event_id=event.event_id,
                        action_value=event.action_value,
                        form_value=event.form_value,
                    )
            else:
                result = card_actions.handle(
                    participant.id,
                    message_id=event.message_id,
                    chat_id=event.chat_id,
                    callback_event_id=event.event_id,
                    action_value=event.action_value,
                    form_value=event.form_value,
                )
        except Exception as exc:
            if receipt_claimed and receipt_fingerprint is not None:
                receipts.complete(
                    event.event_id,
                    action_fingerprint=receipt_fingerprint,
                    status="failed",
                    result_kind="error",
                    error_code="business_failed",
                )
            error_id = uuid.uuid4().hex
            record_failure(
                event,
                summary="CardAction business handling failed",
                error_code="business_failed",
                error_class=type(exc).__name__,
                participant_id=getattr(participant, "id", None),
                error_id=error_id,
                exc_info=True,
            )
            notify_failure(event)
            log_stage(
                event,
                "card_action_completed",
                participant_id=getattr(participant, "id", None),
                result_ok=False,
                error_class=type(exc).__name__,
                error_id=error_id,
            )
            raise

        if not result.get("ok"):
            if receipt_claimed and receipt_fingerprint is not None:
                receipts.complete(
                    event.event_id,
                    action_fingerprint=receipt_fingerprint,
                    status="rejected",
                    result_kind="error",
                    error_code=str(result.get("error") or "action_rejected"),
                )
            error_id = uuid.uuid4().hex
            log_stage(
                event,
                "card_action_business_rejected",
                participant_id=participant.id,
                result_ok=False,
                navigation_only=bool(result.get("navigation_only")),
                error_id=error_id,
            )
            record_failure(
                event,
                summary="CardAction was rejected",
                error_code=str(result.get("error") or "action_rejected"),
                error_class=None,
                participant_id=participant.id,
                severity="warning",
                error_id=error_id,
            )
            notify_failure(event)
            log_stage(
                event,
                "card_action_completed",
                participant_id=participant.id,
                result_ok=False,
                error_id=error_id,
            )
            return {**result, "error_id": error_id}

        spec = card_action_spec(safe_action_name(event))
        navigation_only = bool(
            result.get("navigation_only")
            or (spec is not None and spec.kind == "navigation")
        )
        log_stage(
            event,
            "card_action_business_succeeded",
            participant_id=participant.id,
            result_ok=True,
            navigation_only=navigation_only,
            business_latency_ms=(time.monotonic() - business_started) * 1000,
        )

        backend_state_update = result.get("backend_state_update")
        if (
            backend_state_events is not None
            and not navigation_only
            and isinstance(backend_state_update, dict)
        ):
            try:
                backend_state_events.append(
                    participant.id,
                    backend_state_update,
                )
            except Exception as exc:
                # The business effect is already committed. Preserve that
                # truth and make the missing continuity fact observable rather
                # than rolling back or hiding the completed action.
                logging.getLogger(__name__).exception(
                    "backend_state_event_append_failed event_id=%s participant_id=%s",
                    event.event_id,
                    participant.id,
                )
                if incidents is not None:
                    try:
                        incidents.record(
                            severity="error",
                            subsystem="agent_state_continuity",
                            event_name="backend_state_event_append_failed",
                            summary="Committed CardAction state could not be added to the Agent continuity ledger.",
                            participant_id=participant.id,
                            bot_event_id=event.event_id,
                            error_code="backend_state_event_append_failed",
                            error_class=type(exc).__name__,
                            details={"action_name": safe_action_name(event)},
                        )
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "backend_state_event_incident_record_failed event_id=%s",
                            event.event_id,
                        )

        card = result.get("card")
        if not isinstance(card, dict) or not card:
            card = card_action_result_card(
                message=str(result.get("reply_text") or "已提交")
            )
            result = {**result, "card": card}
        if receipt_claimed and receipt_fingerprint is not None:
            receipts.complete(
                event.event_id,
                action_fingerprint=receipt_fingerprint,
                status="succeeded",
                result_kind="mutation",
            )
        if sender is None:
            log_stage(
                event,
                "card_action_completed",
                participant_id=participant.id,
                result_ok=True,
                navigation_only=navigation_only,
            )
            return result
        update_started = time.monotonic()
        try:
            update_from_callback = getattr(
                sender, "update_card_from_callback", None
            )
            if callable(update_from_callback):
                update_from_callback(
                    getattr(event, "callback_token", None),
                    event.message_id,
                    card,
                )
            else:
                sender.update_card(event.message_id, card)
        except Exception as exc:
            update_error_id = uuid.uuid4().hex
            failure_policy = card_update_failure_policy(exc)
            provider_error_code = failure_policy.provider_error_code
            log_stage(
                event,
                "card_action_update_failed",
                participant_id=participant.id,
                result_ok=True,
                navigation_only=navigation_only,
                card_update_ok=False,
                provider_error_code=provider_error_code,
                error_class=type(exc).__name__,
                error_id=update_error_id,
                card_update_latency_ms=(time.monotonic() - update_started) * 1000,
                total_card_action_latency_ms=(time.monotonic() - total_started) * 1000,
                exc_info=True,
            )
            record_failure(
                event,
                event_name=(
                    "card_action_navigation_update_failed"
                    if navigation_only
                    else "card_action_update_failed_after_commit"
                ),
                summary="CardAction succeeded but source card update failed",
                error_code=(
                    "navigation_update_failed"
                    if navigation_only
                    else "card_update_failed_after_commit"
                ),
                error_class=type(exc).__name__,
                participant_id=participant.id,
                error_id=update_error_id,
                provider_error_code=provider_error_code,
            )
            if not failure_policy.replacement_allowed:
                notify_card_update_failure(event, navigation_only=navigation_only)
                completed = {
                    **result,
                    "card_update_ok": False,
                    "card_replacement_ok": False,
                    "error_id": update_error_id,
                }
                log_stage(
                    event,
                    "card_action_completed",
                    participant_id=participant.id,
                    result_ok=True,
                    navigation_only=navigation_only,
                    card_update_ok=False,
                    error_id=update_error_id,
                )
                return completed
            try:
                sender.send_card(
                    event.chat_id,
                    card,
                    message_uuid=replacement_message_uuid(event.event_id),
                )
            except Exception as replacement_exc:
                replacement_error_id = uuid.uuid4().hex
                replacement_provider_code = getattr(replacement_exc, "code", None)
                log_stage(
                    event,
                    "card_action_replacement_failed",
                    participant_id=participant.id,
                    result_ok=True,
                    navigation_only=navigation_only,
                    card_update_ok=False,
                    provider_error_code=replacement_provider_code,
                    error_class=type(replacement_exc).__name__,
                    error_id=replacement_error_id,
                    exc_info=True,
                )
                notify_card_update_failure(event, navigation_only=navigation_only)
                completed = {
                    **result,
                    "card_update_ok": False,
                    "card_replacement_ok": False,
                    "error_id": replacement_error_id,
                }
                log_stage(
                    event,
                    "card_action_completed",
                    participant_id=participant.id,
                    result_ok=True,
                    navigation_only=navigation_only,
                    card_update_ok=False,
                    error_id=replacement_error_id,
                )
                return completed
            log_stage(
                event,
                "card_action_replacement_succeeded",
                participant_id=participant.id,
                result_ok=True,
                navigation_only=navigation_only,
                card_update_ok=False,
            )
            completed = {
                **result,
                "card_update_ok": False,
                "card_replacement_ok": True,
            }
            log_stage(
                event,
                "card_action_completed",
                participant_id=participant.id,
                result_ok=True,
                navigation_only=navigation_only,
                card_update_ok=False,
            )
            return completed
        log_stage(
            event,
            "card_action_update_succeeded",
            participant_id=participant.id,
            result_ok=True,
            navigation_only=navigation_only,
            card_update_ok=True,
            card_update_latency_ms=(time.monotonic() - update_started) * 1000,
            total_card_action_latency_ms=(time.monotonic() - total_started) * 1000,
        )
        completed = {**result, "card_update_ok": True}
        log_stage(
            event,
            "card_action_completed",
            participant_id=participant.id,
            result_ok=True,
            navigation_only=navigation_only,
            card_update_ok=True,
        )
        return completed

    return handle_card_action


def _build_card_action_executor(
    identity: Any,
    card_actions: Any,
    incidents: Any = None,
    receipts: Any = None,
    backend_state_events: Any = None,
) -> Any:
    """Build the transport-agnostic identity/business/idempotency executor."""

    return _build_card_action_handler(
        identity, card_actions, None, incidents, receipts, backend_state_events
    )


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
    process_started_at = datetime.now(timezone.utc)
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
    from app.integrations.feishu.gateway import BotEvent
    from app.repositories import (
        AgentRunRepository, BindingRepository, BotEventRepository,
        ClaudeSessionRepository, ParticipantRepository, RuntimeIncidentRepository,
    )
    from app.repositories_agent_state import ParticipantAgentStateEventRepository
    from app.services.forecast_scheduler import ForecastScheduler
    from app.services.daily_review_scheduler import DailyReviewScheduler
    from app.services.morning_brief_scheduler import MorningBriefScheduler
    from app.services.reminder_scheduler import ReminderScheduler
    from app.services.supportive_followup_scheduler import SupportiveFollowupScheduler
    from app.services.web_search_maintenance_scheduler import WebSearchMaintenanceScheduler
    from app.services.card_action_receipt_maintenance_scheduler import (
        CardActionReceiptMaintenanceScheduler,
    )
    from app.services.safety_service import SafetyService
    from app.presentation.presentation_agent import ProductionPresentationAgent
    from app.presentation.progress_presenter import ProgressPresenter
    from app.presentation.response_orchestrator import ResponseOrchestrator
    from app.presentation.semantic_segmenter import SemanticSegmenter
    from app.worker import BotWorker
    from app.logging_security import install_credential_redaction
    from app.build_info import announce_build

    settings = Settings.from_env()
    isolate_process_environment()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    install_credential_redaction()
    announce_build("bot")
    _log_web_search_config(settings)
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
    # Start the course import owner before generic Calendar mutation recovery.
    # Forecast recovery is downstream-only and must not get a chance to replay
    # a course provider write first.
    sender = FeishuClient(
        settings.feishu_bot_app_id, settings.feishu_bot_app_secret
    )
    business.course_schedule_import_runner.sender = sender
    business.course_schedule_import_runner.start()
    business.calendar_mutation_plan_runner.sender = sender
    business.calendar_mutation_plan_runner.start()
    business.dependency_refresh.start()
    business.mutation_refresh.start()
    await business.mutation_refresh.recover_startup_fences(process_started_at)
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
        timezone_name=settings.timezone_name,
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
    from app.integrations.feishu.message_resources import (
        FeishuMessageResourceDownloader,
    )

    message_resources = FeishuMessageResourceDownloader(
        sender,
        max_bytes=settings.vision_max_image_bytes,
        timeout_seconds=settings.vision_api_timeout_seconds,
        max_concurrency=settings.vision_max_concurrency,
    )
    from app.repositories_card_action import CardActionReceiptRepository

    card_action_receipts = CardActionReceiptRepository(
        database, ttl_hours=settings.card_action_receipt_ttl_hours
    )
    backend_state_events = ParticipantAgentStateEventRepository(database)
    execute_card_action = _build_card_action_executor(
        identity,
        business.card_actions,
        incidents,
        card_action_receipts,
        backend_state_events,
    )
    from app.card_actions.delivery import build_card_action_delivery

    handle_card_action = build_card_action_delivery(
        execute_card_action, sender, incidents
    )
    sender, gateway = _build_bot_transport(
        settings,
        identity,
        events,
        queue,
        sender=sender,
        card_action_handler=handle_card_action,
    )
    _record_streaming_capability(settings, sender, incidents)
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
        streaming_card_enabled=settings.feishu_streaming_card_enabled,
        streaming_update_interval_ms=settings.feishu_streaming_update_interval_ms,
        streaming_min_chars_per_update=(
            settings.feishu_streaming_min_chars_per_update
        ),
        streaming_max_update_interval_ms=(
            settings.feishu_streaming_max_update_interval_ms
        ),
        streaming_finalize_timeout_seconds=(
            settings.feishu_streaming_finalize_timeout_seconds
        ),
        generic_progress_delay_seconds=(
            settings.generic_progress_delay_seconds
        ),
        tool_progress_grace_seconds=settings.tool_progress_grace_seconds,
        progress_cooldown_seconds=settings.progress_cooldown_seconds,
        progress_max_messages=settings.progress_max_messages,
        incidents=incidents,
        schedule_vision=business.course_schedule_vision,
        generic_image_vision=business.generic_image_vision,
        schedule_imports=business.course_schedule_imports,
        schedule_image_sessions=business.course_schedule_image_sessions,
        message_resources=message_resources,
        schedule_draft_ttl_minutes=settings.vision_import_draft_ttl_minutes,
        schedule_image_max_concurrency=settings.vision_max_concurrency,
        multimodal_debounce_seconds=settings.multimodal_debounce_seconds,
        multimodal_association_seconds=settings.multimodal_association_seconds,
        multimodal_recent_context_seconds=settings.multimodal_recent_context_seconds,
        course_default_semester_start_date=settings.course_default_semester_start_date,
        feature_capabilities={
            "daily_review_enabled": settings.daily_review_enabled,
            "web_search_enabled": settings.web_search_enabled,
        },
        consent_service=business.consent_service,
        memory_service=business.memory,
        interaction_preferences=business.interaction_preferences,
        psychological_context_builder=business.psychological_context,
        backend_state_events=backend_state_events,
        participant_diagnostics_allowlist=(
            settings.participant_diagnostics_allowlist
        ),
    )
    business.course_schedule_tools.recent_image_importer = (
        worker.import_recent_schedule_image
    )
    card_callback = _build_card_callback(settings, execute_card_action)
    card_action_transport_available = _card_action_transport_available(
        settings, card_callback
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
        care_card_enabled=card_action_transport_available,
        care_outcome_refresh=business.care_outcome_refresh,
        proactive_policy=business.proactive_notifications,
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
        proactive_policy=business.proactive_notifications,
    )
    morning_brief_scheduler = MorningBriefScheduler(
        schedules=business.morning_brief_schedules,
        participants=ParticipantRepository(database),
        bindings=bindings,
        care_preferences=business.care_preferences,
        proactive_policy=business.proactive_notifications,
        calendar=business.calendar,
        sender=sender,
        reminder_source=business.reminders,
        timezone_name=settings.timezone_name,
    )
    reminder_scheduler = ReminderScheduler(
        reminders=business.reminders,
        participants=ParticipantRepository(database), bindings=bindings,
        proactive_policy=business.proactive_notifications, sender=sender,
    )
    supportive_followup_scheduler = SupportiveFollowupScheduler(
        candidates=business.followup_candidates,
        participants=ParticipantRepository(database), bindings=bindings,
        policy=business.proactive_notifications, sender=sender,
        support_preferences=business.interaction_preferences,
    )
    web_search_maintenance_scheduler = WebSearchMaintenanceScheduler(
        business.web_search.repository,
        business.public_web_documents.repository,
    )
    card_action_receipt_maintenance_scheduler = (
        CardActionReceiptMaintenanceScheduler(card_action_receipts)
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
                create_time=saved.create_time,
                chat_type=saved.chat_type,
                message_type=saved.message_type,
                text=saved.text,
                image_key=saved.image_key,
            )
        )
    for participant_id in business.device_flows.pending_participants():
        worker.resume_device_flow(participant_id)

    forecast_tasks: asyncio.Task | None = None
    daily_review_tasks: asyncio.Task | None = None
    morning_brief_tasks: asyncio.Task | None = None
    reminder_tasks: asyncio.Task | None = None
    supportive_followup_tasks: asyncio.Task | None = None
    web_search_maintenance_tasks: asyncio.Task | None = None
    card_action_receipt_maintenance_tasks: asyncio.Task | None = None

    async def start_scheduler_after_gateway_ready() -> None:
        nonlocal forecast_tasks, daily_review_tasks, morning_brief_tasks
        nonlocal reminder_tasks, supportive_followup_tasks
        nonlocal web_search_maintenance_tasks
        nonlocal card_action_receipt_maintenance_tasks
        _log_startup_phase("gateway_ready")
        forecast_tasks = asyncio.create_task(
            scheduler.run_forever(), name="forecast-scheduler"
        )
        if _should_start_daily_review_scheduler(
            settings, card_action_transport_available
        ):
            daily_review_tasks = asyncio.create_task(
                daily_review_scheduler.run_forever(), name="daily-review-scheduler"
            )
        morning_brief_tasks = asyncio.create_task(
            morning_brief_scheduler.run_forever(), name="morning-brief-scheduler"
        )
        reminder_tasks = asyncio.create_task(
            reminder_scheduler.run_forever(), name="reminder-scheduler"
        )
        supportive_followup_tasks = asyncio.create_task(
            supportive_followup_scheduler.run_forever(), name="supportive-followup-scheduler"
        )
        web_search_maintenance_tasks = asyncio.create_task(
            web_search_maintenance_scheduler.run_forever(),
            name="web-search-maintenance-scheduler",
        )
        card_action_receipt_maintenance_tasks = asyncio.create_task(
            card_action_receipt_maintenance_scheduler.run_forever(),
            name="card-action-receipt-maintenance-scheduler",
        )
        await scheduler.started.wait()
        if daily_review_tasks is not None:
            await daily_review_scheduler.started.wait()
        await morning_brief_scheduler.started.wait()
        await reminder_scheduler.started.wait()
        await supportive_followup_scheduler.started.wait()
        await web_search_maintenance_scheduler.started.wait()
        await card_action_receipt_maintenance_scheduler.started.wait()
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
            await morning_brief_scheduler.close()
            await reminder_scheduler.close()
            await supportive_followup_scheduler.close()
            await web_search_maintenance_scheduler.close()
            await card_action_receipt_maintenance_scheduler.close()
            if forecast_tasks is not None:
                forecast_tasks.cancel()
            if daily_review_tasks is not None:
                daily_review_tasks.cancel()
            if morning_brief_tasks is not None:
                morning_brief_tasks.cancel()
            if reminder_tasks is not None:
                reminder_tasks.cancel()
            if supportive_followup_tasks is not None:
                supportive_followup_tasks.cancel()
            if web_search_maintenance_tasks is not None:
                web_search_maintenance_tasks.cancel()
            if card_action_receipt_maintenance_tasks is not None:
                card_action_receipt_maintenance_tasks.cancel()
            dispatcher.cancel()
            scheduler_tasks = (
                forecast_tasks,
                daily_review_tasks,
                morning_brief_tasks,
                reminder_tasks,
                supportive_followup_tasks,
                web_search_maintenance_tasks,
                card_action_receipt_maintenance_tasks,
            )
            await asyncio.gather(
                dispatcher,
                *(task for task in scheduler_tasks if task is not None),
                return_exceptions=True,
            )
            try:
                await worker.close()
            finally:
                try:
                    await business.course_schedule_import_runner.close()
                finally:
                    try:
                        await business.calendar_mutation_plan_runner.close()
                    finally:
                        try:
                            await business.mutation_refresh.close()
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
