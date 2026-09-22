"""Unified backend-owned Feishu sender. It is never an Agent tool."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from urllib.parse import quote
import uuid


class FeishuSendError(RuntimeError):
    def __init__(
        self, message: str, *, code: int | None = None,
        retryable: bool = True, operation: str = "send_message",
        replacement_allowed: bool = False,
        provider_request_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.operation = operation
        self.replacement_allowed = bool(replacement_allowed)
        self.provider_request_id = (
            str(provider_request_id)[:128] if provider_request_id else None
        )
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

    def send_card(
        self, chat_id: str, card: dict[str, Any], *, message_uuid: str | None = None
    ) -> str:
        if not isinstance(card, dict) or not card:
            raise ValueError("Feishu card must be a non-empty object")
        if message_uuid is None:
            return self._send_message(chat_id, "interactive", card)
        return self._send_message(chat_id, "interactive", card, message_uuid=message_uuid)

    def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            raise ValueError("Feishu message_id is required")
        if not isinstance(card, dict) or not card:
            raise ValueError("Feishu card must be a non-empty object")
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        body = (
            PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            PatchMessageRequest.builder()
            .message_id(normalized_message_id)
            .request_body(body)
            .build()
        )
        try:
            response = self._client.im.v1.message.patch(request)
        except Exception as exc:
            raise FeishuSendError(
                "Feishu card update request failed", operation="update_card"
            ) from exc
        if not response or not response.success():
            code = getattr(response, "code", None)
            retryable = code not in {230001, 230003, 230006, 99991672}
            raise FeishuSendError(
                str(getattr(response, "msg", "Feishu card update failed")),
                code=code,
                retryable=retryable,
                operation="update_card",
                provider_request_id=self._response_request_id(response),
                # These provider responses mean the original message target
                # is gone or can no longer be patched. Authentication and
                # generic validation failures must not create another card.
                replacement_allowed=code in {230003, 230006},
            )

    def create_card_instance(self, card: dict[str, Any]) -> str:
        if not isinstance(card, dict) or not card:
            raise ValueError("Feishu CardKit card must be a non-empty object")
        response = self._cardkit_request(
            "POST",
            "/open-apis/cardkit/v1/cards",
            {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)},
            operation="create_card_instance",
        )
        data = self._cardkit_response_data(response, operation="create_card_instance")
        card_id = data.get("card_id") if isinstance(data, dict) else None
        if not card_id:
            raise FeishuSendError(
                "Feishu CardKit response has no card_id",
                retryable=False,
                operation="create_card_instance",
            )
        return str(card_id)

    def send_card_by_reference(
        self,
        chat_id: str,
        card_id: str,
        *,
        message_uuid: str | None = None,
    ) -> str:
        reference = {"type": "card", "data": {"card_id": str(card_id)}}
        return self._send_message(
            chat_id,
            "interactive",
            reference,
            message_uuid=message_uuid,
        )

    def update_card_element_content(
        self,
        card_id: str,
        element_id: str,
        content: str,
        sequence: int,
    ) -> None:
        from app.integrations.feishu.streaming_card import (
            validate_cardkit_element_id,
        )

        normalized_element_id = validate_cardkit_element_id(element_id)
        self._cardkit_request(
            "PUT",
            "/open-apis/cardkit/v1/cards/"
            f"{quote(str(card_id), safe='')}/elements/"
            f"{quote(normalized_element_id, safe='')}/content",
            {
                "content": str(content),
                "sequence": int(sequence),
                "uuid": self._cardkit_operation_uuid(
                    "content", card_id, normalized_element_id, sequence
                ),
            },
            operation="update_card_element_content",
        )

    def finish_streaming_card(self, card_id: str, sequence: int) -> None:
        self._cardkit_request(
            "PATCH",
            f"/open-apis/cardkit/v1/cards/{quote(str(card_id), safe='')}/settings",
            {
                "settings": json.dumps(
                    {"config": {"streaming_mode": False}}, ensure_ascii=False
                ),
                "sequence": int(sequence),
                "uuid": self._cardkit_operation_uuid(
                    "settings", card_id, "", sequence
                ),
            },
            operation="finish_streaming_card",
        )

    async def start_streaming_card(
        self,
        chat_id: str,
        initial_content: str = "正在整理结果…",
        *,
        message_uuid: str | None = None,
        update_interval_ms: int = 120,
        min_update_chars: int = 30,
        max_update_interval_ms: int = 300,
    ):
        import asyncio

        from app.integrations.feishu.streaming_card import (
            ANSWER_ELEMENT_ID,
            FeishuStreamingCardSession,
            streaming_answer_card,
        )

        card_id = await asyncio.to_thread(
            self.create_card_instance,
            streaming_answer_card(initial_content),
        )
        try:
            message_id = await asyncio.to_thread(
                self.send_card_by_reference,
                chat_id,
                card_id,
                message_uuid=message_uuid,
            )
        except Exception:
            try:
                await asyncio.to_thread(self.finish_streaming_card, card_id, 1)
            except Exception:
                pass
            raise
        return FeishuStreamingCardSession(
            client=self,
            card_id=card_id,
            message_id=message_id,
            element_id=ANSWER_ELEMENT_ID,
            visible_content=str(initial_content),
            update_interval_ms=update_interval_ms,
            min_update_chars=min_update_chars,
            max_update_interval_ms=max_update_interval_ms,
        )

    @staticmethod
    def _cardkit_operation_uuid(
        operation: str,
        card_id: str,
        element_id: str,
        sequence: int,
    ) -> str:
        return str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            "mindflow-cardkit:"
            f"{operation}:{card_id}:{element_id}:{int(sequence)}",
        ))

    def _cardkit_request(
        self,
        method: str,
        uri: str,
        body: dict[str, Any],
        *,
        operation: str,
    ):
        from lark_oapi.core.const import APPLICATION_JSON, CONTENT_TYPE
        from lark_oapi.core.enum import AccessTokenType, HttpMethod
        from lark_oapi.core.model import BaseRequest

        http_method = {
            "POST": HttpMethod.POST,
            "PUT": HttpMethod.PUT,
            "PATCH": HttpMethod.PATCH,
        }[method]
        request = (
            BaseRequest.builder()
            .http_method(http_method)
            .uri(uri)
            .token_types({AccessTokenType.TENANT})
            .headers({CONTENT_TYPE: f"{APPLICATION_JSON}; charset=utf-8"})
            .body(body)
            .build()
        )
        try:
            response = self._client.request(request)
        except Exception as exc:
            raise FeishuSendError(
                f"Feishu CardKit {operation} request failed",
                operation=operation,
            ) from exc
        if not response or not response.success():
            code = getattr(response, "code", None)
            raise FeishuSendError(
                str(getattr(response, "msg", f"Feishu CardKit {operation} failed")),
                code=code,
                retryable=code not in {230001, 230003, 230006, 99991672},
                operation=operation,
                provider_request_id=self._response_request_id(response),
            )
        return response

    @staticmethod
    def _cardkit_response_data(response: Any, *, operation: str) -> dict[str, Any] | None:
        """Return CardKit response data across typed and generic SDK responses.

        Some lark-oapi versions leave ``BaseResponse.data`` empty even though the
        raw HTTP response contains the successful CardKit payload.  Prefer the
        raw payload when it is present so that the generic ``BaseRequest`` path
        does not turn a successful card creation into a false failure.
        """

        raw = getattr(response, "raw", None)
        raw_content = getattr(raw, "content", None) if raw is not None else None
        if raw_content is not None:
            try:
                if isinstance(raw_content, (bytes, bytearray)):
                    raw_content = bytes(raw_content).decode("utf-8")
                payload = json.loads(str(raw_content))
            except (UnicodeDecodeError, TypeError, ValueError) as exc:
                raise FeishuSendError(
                    "Feishu CardKit response has invalid JSON",
                    retryable=False,
                    operation=operation,
                ) from exc
            if not isinstance(payload, dict):
                raise FeishuSendError(
                    "Feishu CardKit response payload is not an object",
                    retryable=False,
                    operation=operation,
                )
            if payload.get("code") != 0:
                raise FeishuSendError(
                    "Feishu CardKit response has a non-zero code",
                    code=payload.get("code") if isinstance(payload.get("code"), int) else None,
                    retryable=False,
                    operation=operation,
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise FeishuSendError(
                    "Feishu CardKit response data is not an object",
                    retryable=False,
                    operation=operation,
                )
            return data

        data = getattr(response, "data", None)
        if isinstance(data, dict):
            return data
        if data is None:
            return None
        card_id = getattr(data, "card_id", None)
        return {"card_id": card_id} if card_id is not None else None

    @staticmethod
    def _response_request_id(response: Any) -> str | None:
        request_id = getattr(response, "request_id", None)
        if not request_id:
            getter = getattr(response, "get_request_id", None)
            if callable(getter):
                try:
                    request_id = getter()
                except Exception:
                    request_id = None
        return str(request_id)[:128] if request_id else None

    def update_card_from_callback(
        self,
        callback_token: str | None,
        message_id: str,
        card: dict[str, Any],
    ) -> None:
        """Update a callback card exactly once using the strongest target."""

        normalized_token = str(callback_token or "").strip()
        if not normalized_token:
            self.update_card(message_id, card)
            return
        if not isinstance(card, dict) or not card:
            raise ValueError("Feishu card must be a non-empty object")

        from lark_oapi.core.const import APPLICATION_JSON, CONTENT_TYPE
        from lark_oapi.core.enum import AccessTokenType, HttpMethod
        from lark_oapi.core.model import BaseRequest

        request = (
            BaseRequest.builder()
            .http_method(HttpMethod.POST)
            .uri("/open-apis/interactive/v1/card/update")
            .token_types({AccessTokenType.TENANT})
            .headers({CONTENT_TYPE: f"{APPLICATION_JSON}; charset=utf-8"})
            .body({"token": normalized_token, "card": card})
            .build()
        )
        try:
            response = self._client.request(request)
        except Exception as exc:
            raise FeishuSendError(
                "Feishu delayed card update request failed",
                operation="update_card_from_callback",
            ) from exc
        if not response or not response.success():
            code = getattr(response, "code", None)
            if str(code) == "300090":
                # The callback token can be invalid even while the original
                # message remains patchable.  This is a target-resolution
                # failure, so retry exactly once against the same message ID;
                # other provider errors must keep their existing failure
                # semantics.
                self.update_card(message_id, card)
                return
            raise FeishuSendError(
                str(getattr(response, "msg", "Feishu delayed card update failed")),
                code=code,
                retryable=code not in {230001, 99991672},
                operation="update_card_from_callback",
                # A callback-token failure does not establish that creating a
                # second message is safe or useful.
                replacement_allowed=False,
            )

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

    def download_message_image(
        self, message_id: str, image_key: str, *, max_bytes: int | None = None
    ) -> bytes:
        """Download an inbound image through Feishu's message-resource API."""

        normalized_message_id = str(message_id or "").strip()
        normalized_image_key = str(image_key or "").strip()
        if not normalized_message_id or not normalized_image_key:
            raise ValueError("message_id and image_key are required")
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = (
            GetMessageResourceRequest.builder()
            .message_id(normalized_message_id)
            .file_key(normalized_image_key)
            .type("image")
            .build()
        )
        try:
            response = self._client.im.v1.message_resource.get(request)
        except Exception as exc:
            raise FeishuSendError(
                "Feishu message image download failed",
                operation="download_message_image",
            ) from exc
        if not response or not response.success():
            code = getattr(response, "code", None)
            raise FeishuSendError(
                "Feishu message image download failed",
                code=code,
                retryable=code not in {230001, 230003, 230006, 99991672},
                operation="download_message_image",
            )
        file_obj = getattr(response, "file", None)
        if not hasattr(file_obj, "read"):
            raise FeishuSendError(
                "Feishu message image response has no file",
                operation="download_message_image",
            )
        read_limit = int(max_bytes) + 1 if max_bytes is not None else -1
        return bytes(file_obj.read(read_limit))

    def _send_message(
        self, chat_id: str, msg_type: str, content: dict[str, Any], *,
        message_uuid: str | None = None,
    ) -> str:
        normalized_uuid = str(message_uuid or "").strip()
        if len(normalized_uuid) > 50:
            raise ValueError("Feishu message_uuid must be at most 50 characters")
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body_builder = (
            CreateMessageRequestBody.builder()
            .receive_id(str(chat_id))
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
        )
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
                provider_request_id=self._response_request_id(response),
            )
        message_id = str(getattr(getattr(response, "data", None), "message_id", ""))
        if not message_id:
            raise FeishuSendError("Feishu response has no message_id")
        return message_id
