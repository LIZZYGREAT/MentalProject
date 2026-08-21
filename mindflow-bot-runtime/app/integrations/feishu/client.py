"""Unified backend-owned Feishu sender. It is never an Agent tool."""

from __future__ import annotations

import json
from typing import Any


class FeishuSendError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, *, sdk_client: Any = None):
        self.app_id = app_id
        self.app_secret = app_secret
        if sdk_client is not None:
            self._client = sdk_client
            return
        import lark_oapi as lark

        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def send_text(self, chat_id: str, text: str) -> str:
        return self._send_message(
            chat_id, "text", {"text": str(text)}
        )

    def send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        if not isinstance(card, dict) or not card:
            raise ValueError("Feishu card must be a non-empty object")
        return self._send_message(chat_id, "interactive", card)

    def _send_message(
        self, chat_id: str, msg_type: str, content: dict[str, Any]
    ) -> str:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(str(chat_id))
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
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
            raise FeishuSendError("Feishu send request failed") from exc
        if not response or not response.success():
            code = getattr(response, "code", None)
            retryable = code not in {230001, 230003, 230006, 99991672}
            raise FeishuSendError(
                str(getattr(response, "msg", "Feishu send failed")),
                code=code,
                retryable=retryable,
            )
        message_id = str(getattr(getattr(response, "data", None), "message_id", ""))
        if not message_id:
            raise FeishuSendError("Feishu response has no message_id")
        return message_id
