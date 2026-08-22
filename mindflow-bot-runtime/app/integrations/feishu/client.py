"""Unified backend-owned Feishu sender. It is never an Agent tool."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any


class FeishuSendError(RuntimeError):
    def __init__(
        self, message: str, *, code: int | None = None,
        retryable: bool = True, operation: str = "send_message",
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.operation = operation
        self.error_class = type(self).__name__


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

    def send_text(
        self, chat_id: str, text: str, *, message_uuid: str | None = None,
    ) -> str:
        if message_uuid is None:
            return self._send_message(chat_id, "text", {"text": str(text)})
        return self._send_message(
            chat_id, "text", {"text": str(text)}, message_uuid=message_uuid,
        )

    def send_card(self, chat_id: str, card: dict[str, Any]) -> str:
        if not isinstance(card, dict) or not card:
            raise ValueError("Feishu card must be a non-empty object")
        return self._send_message(chat_id, "interactive", card)

    def upload_image(self, png_bytes: bytes) -> str:
        if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
            raise ValueError("Feishu image upload requires non-empty bytes")
        if not bytes(png_bytes).startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Feishu pressure image must be PNG")
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        body = (
            CreateImageRequestBody.builder()
            .image_type("message")
            .image(BytesIO(bytes(png_bytes)))
            .build()
        )
        request = CreateImageRequest.builder().request_body(body).build()
        try:
            response = self._client.im.v1.image.create(request)
        except Exception as exc:
            raise FeishuSendError(
                "Feishu image upload request failed", operation="upload_image"
            ) from exc
        if not response or not response.success():
            code = getattr(response, "code", None)
            retryable = code not in {230001, 230003, 230006, 99991672}
            raise FeishuSendError(
                str(getattr(response, "msg", "Feishu image upload failed")),
                code=code,
                retryable=retryable,
                operation="upload_image",
            )
        image_key = str(getattr(getattr(response, "data", None), "image_key", ""))
        if not image_key:
            raise FeishuSendError(
                "Feishu image upload response has no image_key",
                operation="upload_image",
            )
        return image_key

    def send_image(self, chat_id: str, image_key: str) -> str:
        normalized = str(image_key or "").strip()
        if not normalized:
            raise ValueError("Feishu image_key is required")
        return self._send_message(chat_id, "image", {"image_key": normalized})

    def _send_message(
        self, chat_id: str, msg_type: str, content: dict[str, Any], *,
        message_uuid: str | None = None,
    ) -> str:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body_builder = (
            CreateMessageRequestBody.builder()
            .receive_id(str(chat_id))
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
        )
        normalized_uuid = str(message_uuid or "").strip()
        if normalized_uuid:
            body_builder = body_builder.uuid(normalized_uuid)
        body = body_builder.build()
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
