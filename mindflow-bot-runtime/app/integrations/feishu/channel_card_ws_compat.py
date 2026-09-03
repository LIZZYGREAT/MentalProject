"""Guarded compatibility for CARD frames dropped by lark-channel-sdk 1.2.0."""

from __future__ import annotations

import inspect
from importlib import metadata
from typing import Any


_SUPPORTED_VERSION = "1.2.0"
_CONTRACT_ERROR = "unsupported lark-channel-sdk CARD transport contract"


def _reject() -> None:
    raise RuntimeError(_CONTRACT_ERROR)


def install_card_ws_compat() -> None:
    """Route CARD frames through the SDK dispatcher without changing other frames.

    This is a temporary compatibility shim for the upstream CARD-frame drop in
    lark-channel-sdk 1.2.0. It may be removed only after a newer SDK passes the
    automated CARD dispatch/response-writeback regression suite and a real
    MindFlow long-connection CardAction smoke test (including Part 6C).
    """

    try:
        version = metadata.version("lark-channel-sdk")
        import lark_channel.ws.client as ws_module

        client_type = ws_module.Client
        original = client_type._handle_data_frame
        already_installed = bool(
            getattr(original, "__mindflow_card_ws_compat__", False)
        )
        if version != _SUPPORTED_VERSION:
            _reject()
        if already_installed:
            return
        signature = inspect.signature(original)
        parameters = list(signature.parameters.values())
        required_module_attributes = (
            "MessageType",
            "Response",
            "HEADER_MESSAGE_ID",
            "HEADER_TRACE_ID",
            "HEADER_SUM",
            "HEADER_SEQ",
            "HEADER_TYPE",
            "HEADER_BIZ_RT",
            "JSON",
            "UTF_8",
            "base64",
            "http",
            "time",
            "logger",
            "_get_by_key",
        )
        required_client_attributes = ("_combine", "_fmt_log", "_write_message")
        source = inspect.getsource(original)
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        _reject()

    if (
        not inspect.iscoroutinefunction(original)
        or [parameter.name for parameter in parameters] != ["self", "frame"]
        or any(not hasattr(ws_module, name) for name in required_module_attributes)
        or any(not hasattr(client_type, name) for name in required_client_attributes)
        or not hasattr(ws_module.MessageType, "EVENT")
        or not hasattr(ws_module.MessageType, "CARD")
        or "elif message_type == MessageType.CARD:" not in source
        or "self._event_handler._do_without_validation(pl)" not in source
    ):
        _reject()

    async def _handle_data_frame(self: Any, frame: Any) -> None:
        hs = frame.headers
        msg_id = ws_module._get_by_key(hs, ws_module.HEADER_MESSAGE_ID)
        trace_id = ws_module._get_by_key(hs, ws_module.HEADER_TRACE_ID)
        sum_ = ws_module._get_by_key(hs, ws_module.HEADER_SUM)
        seq = ws_module._get_by_key(hs, ws_module.HEADER_SEQ)
        type_ = ws_module._get_by_key(hs, ws_module.HEADER_TYPE)

        payload = frame.payload
        if int(sum_) > 1:
            payload = self._combine(msg_id, int(sum_), int(seq), payload)
            if payload is None:
                return

        message_type = ws_module.MessageType(type_)
        ws_module.logger.debug(
            self._fmt_log(
                "receive message, message_type: {}, message_id: {}, "
                "trace_id: {}, payload_len: {}",
                message_type.value,
                msg_id,
                trace_id,
                len(payload),
            )
        )

        response = ws_module.Response(code=ws_module.http.HTTPStatus.OK)
        try:
            start = int(round(ws_module.time.time() * 1000))
            if message_type in (
                ws_module.MessageType.EVENT,
                ws_module.MessageType.CARD,
            ):
                result = self._event_handler._do_without_validation(payload)
            else:
                return
            end = int(round(ws_module.time.time() * 1000))
            header = hs.add()
            header.key = ws_module.HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                response.data = ws_module.base64.b64encode(
                    ws_module.JSON.marshal(result).encode(ws_module.UTF_8)
                )
        except Exception as exc:
            ws_module.logger.error(
                self._fmt_log(
                    "handle message failed, message_type: {}, message_id: {}, "
                    "trace_id: {}, err: {}",
                    message_type.value,
                    msg_id,
                    trace_id,
                    exc,
                )
            )
            response = ws_module.Response(
                code=ws_module.http.HTTPStatus.INTERNAL_SERVER_ERROR
            )

        frame.payload = ws_module.JSON.marshal(response).encode(ws_module.UTF_8)
        await self._write_message(frame.SerializeToString())

    _handle_data_frame.__mindflow_card_ws_compat__ = True
    _handle_data_frame.__mindflow_original__ = original
    client_type._handle_data_frame = _handle_data_frame
