"""Normalize Feishu SDK event envelopes into a minimal persistent shape."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional


class InvalidFeishuEvent(ValueError):
    """Raised when a provider event cannot be safely queued."""


def _nested(value: Dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


class FeishuEventParser:
    """Parse message and card-action payloads without running business logic."""

    def __init__(self, app_id: str):
        self.app_id = str(app_id or "").strip()

    def parse_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        header = payload.get("header") or {}
        event = payload.get("event") or payload
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        if str(sender.get("sender_type") or "").lower() in {"app", "bot"}:
            raise InvalidFeishuEvent("忽略机器人自身消息")

        open_id = _first(sender_id.get("open_id"), sender.get("open_id"))
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        if not open_id or not message_id or not chat_id:
            raise InvalidFeishuEvent("消息事件缺少必要标识")

        message_type = str(message.get("message_type") or "unknown")
        raw_content = message.get("content")
        content: Dict[str, Any]
        if isinstance(raw_content, str):
            try:
                decoded = json.loads(raw_content)
                content = decoded if isinstance(decoded, dict) else {"value": decoded}
            except json.JSONDecodeError:
                content = {"text": raw_content}
        elif isinstance(raw_content, dict):
            content = dict(raw_content)
        else:
            content = {}
        if message_type == "text":
            content = {"text": str(content.get("text") or "")[:4000]}
        else:
            # Unsupported message bodies are not needed after routing.
            content = {"unsupported_type": message_type}

        event_id = _first(header.get("event_id"), payload.get("event_id"), message_id)
        return {
            "event_id": str(event_id),
            "message_id": str(message_id),
            "app_id": self.app_id,
            "tenant_key": str(header.get("tenant_key") or ""),
            "sender_open_id": str(open_id),
            "chat_id": str(chat_id),
            "chat_type": str(message.get("chat_type") or "p2p"),
            "message_type": message_type,
            "event_type": "message",
            "content": content,
        }

    def parse_card_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        header = payload.get("header") or {}
        event = payload.get("event") or payload
        operator = event.get("operator") or {}
        operator_id = operator.get("operator_id") or {}
        context = event.get("context") or {}
        action = event.get("action") or {}
        open_id = _first(operator_id.get("open_id"), operator.get("open_id"))
        chat_id = _first(
            context.get("open_chat_id"),
            context.get("chat_id"),
            event.get("open_chat_id"),
        )
        if not open_id or not chat_id:
            raise InvalidFeishuEvent("卡片事件缺少操作者或会话标识")

        action_value = action.get("value") or {}
        if not isinstance(action_value, dict):
            action_value = {"value": action_value}
        form_value = action.get("form_value") or event.get("form_value") or {}
        if isinstance(form_value, dict):
            action_value = {**action_value, **form_value}
        content = {
            "action": action_value,
            "action_name": str(action.get("name") or action_value.get("action") or ""),
        }
        provider_message_id = _first(
            context.get("open_message_id"),
            context.get("message_id"),
        )
        event_id = _first(header.get("event_id"), payload.get("event_id"))
        if not event_id:
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "open_id": open_id,
                        "message_id": provider_message_id,
                        "action": content,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            event_id = f"card_{digest}"
        return {
            "event_id": str(event_id),
            "message_id": None,
            "app_id": self.app_id,
            "tenant_key": str(header.get("tenant_key") or ""),
            "sender_open_id": str(open_id),
            "chat_id": str(chat_id),
            "chat_type": "p2p",
            "message_type": "interactive",
            "event_type": "card_action",
            "content": content,
        }
