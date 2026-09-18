"""Conservative delivery-failure classification for CardAction results."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import time
from typing import Any
import uuid


@dataclass(frozen=True)
class CardUpdateFailurePolicy:
    retryable: bool
    replacement_allowed: bool
    provider_error_code: int | None
    operation: str


def card_update_failure_policy(exc: BaseException) -> CardUpdateFailurePolicy:
    raw_code: Any = getattr(exc, "code", None)
    try:
        provider_error_code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        provider_error_code = None
    return CardUpdateFailurePolicy(
        retryable=bool(getattr(exc, "retryable", False)),
        replacement_allowed=bool(getattr(exc, "replacement_allowed", False)),
        provider_error_code=provider_error_code,
        operation=str(getattr(exc, "operation", "unknown") or "unknown")[:64],
    )


def build_card_action_delivery(executor: Any, sender: Any, incidents: Any = None) -> Any:
    """Wrap a transport-agnostic executor with WS delayed delivery."""

    from app.card_actions.registry import card_action_spec

    logger = logging.getLogger(__name__)

    def action_name(event: Any) -> str | None:
        candidate = str((event.action_value or {}).get("mindflow_action") or "")
        return candidate if card_action_spec(candidate) is not None else None

    def notify(event: Any, text: str) -> None:
        try:
            sender.send_text(event.chat_id, text)
        except Exception:
            logger.exception(
                "feishu_card_action_update_notice_failed event_id=%s message_id=%s",
                event.event_id,
                event.message_id,
            )

    def record_update_failure(
        event: Any,
        *,
        navigation_only: bool,
        exc: BaseException,
        error_id: str,
        provider_error_code: int | None,
    ) -> None:
        event_name = (
            "card_action_navigation_update_failed"
            if navigation_only
            else "card_action_update_failed_after_commit"
        )
        logger.error(
            "%s event_id=%s message_id=%s action_name=%s "
            "provider_error_code=%s error_class=%s error_id=%s",
            event_name,
            event.event_id,
            event.message_id,
            action_name(event),
            provider_error_code,
            type(exc).__name__,
            error_id,
        )
        if incidents is None:
            return
        try:
            incidents.record(
                severity="error",
                subsystem="feishu_card_action",
                event_name=event_name,
                summary="CardAction succeeded but source card update failed",
                participant_id=None,
                bot_event_id=None,
                error_code=(
                    "navigation_update_failed"
                    if navigation_only
                    else "card_update_failed_after_commit"
                ),
                error_class=type(exc).__name__,
                details={
                    "message_id": event.message_id,
                    "action_tag": event.action_tag,
                    "action_name": action_name(event),
                    "callback_event_id": event.event_id,
                    "error_id": error_id,
                    "provider_error_code": provider_error_code,
                },
            )
        except Exception:
            logger.exception(
                "feishu_card_action_incident_record_failed event_id=%s "
                "message_id=%s incident_event=%s",
                event.event_id,
                event.message_id,
                event_name,
            )

    def deliver(event: Any) -> dict[str, Any]:
        total_started = time.monotonic()
        try:
            result = executor(event)
        except Exception:
            notify(event, "操作未能完成，请稍后重试。")
            raise
        if not result.get("ok"):
            if result.get("error") != "card_action_processing":
                notify(event, "操作未能完成，请稍后重试。")
            return result

        spec = card_action_spec(action_name(event))
        navigation_only = bool(
            result.get("navigation_only")
            or (spec is not None and spec.kind == "navigation")
        )
        card = result["card"]
        update_started = time.monotonic()
        try:
            update_from_callback = getattr(sender, "update_card_from_callback", None)
            if callable(update_from_callback):
                update_from_callback(
                    getattr(event, "callback_token", None), event.message_id, card
                )
            else:
                sender.update_card(event.message_id, card)
        except Exception as exc:
            policy = card_update_failure_policy(exc)
            error_id = uuid.uuid4().hex
            update_latency = (time.monotonic() - update_started) * 1000
            record_update_failure(
                event,
                navigation_only=navigation_only,
                exc=exc,
                error_id=error_id,
                provider_error_code=policy.provider_error_code,
            )
            logger.error(
                "card_action_update_failed event_id=%s message_id=%s "
                "action_name=%s card_update_latency_ms=%.3f "
                "total_card_action_latency_ms=%.3f",
                event.event_id,
                event.message_id,
                action_name(event),
                update_latency,
                (time.monotonic() - total_started) * 1000,
            )
            if not policy.replacement_allowed:
                notify(
                    event,
                    "功能卡暂时没能更新，重新发“功能”即可。"
                    if navigation_only
                    else "操作已经完成，但卡片状态暂未更新，无需重复点击。",
                )
                return {
                    **result,
                    "card_update_ok": False,
                    "card_replacement_ok": False,
                    "error_id": error_id,
                }
            replacement_uuid = "mindflow-" + hashlib.sha256(
                f"mindflow:card-action-result:{event.event_id}".encode("utf-8")
            ).hexdigest()[:40]
            try:
                sender.send_card(
                    event.chat_id, card, message_uuid=replacement_uuid
                )
            except Exception as replacement_exc:
                replacement_error_id = uuid.uuid4().hex
                logger.exception(
                    "card_action_replacement_failed event_id=%s message_id=%s "
                    "provider_error_code=%s error_class=%s error_id=%s",
                    event.event_id,
                    event.message_id,
                    getattr(replacement_exc, "code", None),
                    type(replacement_exc).__name__,
                    replacement_error_id,
                )
                notify(
                    event,
                    "功能卡暂时没能更新，重新发“功能”即可。"
                    if navigation_only
                    else "操作已经完成，但卡片状态暂未更新，无需重复点击。",
                )
                return {
                    **result,
                    "card_update_ok": False,
                    "card_replacement_ok": False,
                    "error_id": replacement_error_id,
                }
            return {
                **result,
                "card_update_ok": False,
                "card_replacement_ok": True,
            }

        logger.info(
            "card_action_update_succeeded event_id=%s message_id=%s "
            "action_name=%s card_update_latency_ms=%.3f "
            "total_card_action_latency_ms=%.3f",
            event.event_id,
            event.message_id,
            action_name(event),
            (time.monotonic() - update_started) * 1000,
            (time.monotonic() - total_started) * 1000,
        )
        return {**result, "card_update_ok": True}

    return deliver
