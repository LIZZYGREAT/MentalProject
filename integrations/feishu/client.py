"""Small outbound Feishu client with test-friendly dependency injection."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


class FeishuSendError(RuntimeError):
    def __init__(self, message: str, *, code: Optional[int] = None, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class FeishuBotClient:
    """Send text or interactive messages with the application tenant identity."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        sdk_client: Any = None,
    ):
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        if sdk_client is not None:
            self._client = sdk_client
            return
        if not self.app_id or not self.app_secret:
            raise ValueError("机器人启用时必须配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        try:
            import lark_oapi as lark
        except ImportError as exc:  # pragma: no cover - deployment guard
            raise RuntimeError("缺少 lark-oapi，请先安装 requirements.txt") from exc
        self._client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def send_text(self, chat_id: str, text: str) -> str:
        return self.send(chat_id, "text", {"text": str(text)})

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> str:
        return self.send(chat_id, "interactive", card)

    def send(self, chat_id: str, msg_type: str, content: Dict[str, Any]) -> str:
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )
        except ImportError as exc:  # pragma: no cover - deployment guard
            raise RuntimeError("缺少 lark-oapi，请先安装 requirements.txt") from exc

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(str(chat_id))
            .msg_type(str(msg_type))
            .content(json.dumps(content, ensure_ascii=False, separators=(",", ":")))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        try:
            response = self._client.im.v1.message.create(request)
        except Exception as exc:
            raise FeishuSendError("飞书消息发送请求失败", retryable=True) from exc
        if not response or not response.success():
            code = getattr(response, "code", None)
            message = str(getattr(response, "msg", None) or "飞书消息发送失败")
            retryable = code not in {230001, 230003, 230006, 99991672}
            raise FeishuSendError(message, code=code, retryable=retryable)
        data = getattr(response, "data", None)
        message_id = str(getattr(data, "message_id", "") or "")
        if not message_id:
            raise FeishuSendError("飞书响应缺少 message_id", retryable=True)
        return message_id
