"""Standalone Feishu WebSocket gateway; callbacks only normalize and enqueue."""

from __future__ import annotations

import json
import os

from auth.database import AppDatabase
from integrations.feishu.events import FeishuEventParser, InvalidFeishuEvent


def _payload_from_sdk(lark, data) -> dict:
    raw = lark.JSON.marshal(data)
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    raise InvalidFeishuEvent("无法解析飞书事件")


def build_event_handler(database: AppDatabase, app_id: str, lark):
    parser = FeishuEventParser(app_id)

    def enqueue_message(data) -> None:
        try:
            database.enqueue_feishu_event(
                parser.parse_message(_payload_from_sdk(lark, data))
            )
            database.update_bot_heartbeat("feishu_bot", "online", {"last_event": "message"})
        except InvalidFeishuEvent:
            return

    def enqueue_card_action(data):
        try:
            database.enqueue_feishu_event(
                parser.parse_card_action(_payload_from_sdk(lark, data))
            )
            database.update_bot_heartbeat("feishu_bot", "online", {"last_event": "card_action"})
        except InvalidFeishuEvent:
            pass
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        return P2CardActionTriggerResponse(
            {"toast": {"type": "info", "content": "已收到，正在处理"}}
        )

    builder = lark.EventDispatcherHandler.builder(
        os.getenv("FEISHU_ENCRYPT_KEY", ""),
        os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
        lark.LogLevel.INFO,
    ).register_p2_im_message_receive_v1(enqueue_message)
    if hasattr(builder, "register_p2_card_action_trigger"):
        builder = builder.register_p2_card_action_trigger(enqueue_card_action)
    return builder.build()


def main() -> None:
    if os.getenv("FEISHU_BOT_ENABLED", "false").lower() != "true":
        return
    if os.getenv("FEISHU_BOT_TRANSPORT", "websocket").lower() != "websocket":
        raise RuntimeError("第一版仅支持 FEISHU_BOT_TRANSPORT=websocket")
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    bind_base_url = os.getenv("FEISHU_BIND_BASE_URL", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("机器人启用时必须配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    if os.getenv("APP_ENV", "development").lower() == "production" and not bind_base_url.startswith(
        "https://"
    ):
        raise RuntimeError("生产环境 FEISHU_BIND_BASE_URL 必须使用 HTTPS")
    try:
        import lark_oapi as lark
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise RuntimeError("缺少 lark-oapi，请先安装 requirements.txt") from exc

    database = AppDatabase()
    database.init_schema()
    handler = build_event_handler(database, app_id, lark)
    database.update_bot_heartbeat("feishu_bot", "connecting", {"transport": "websocket"})
    try:
        client = lark.ws.Client(
            app_id,
            app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        database.update_bot_heartbeat("feishu_bot", "online", {"transport": "websocket"})
        client.start()
    finally:
        database.update_bot_heartbeat("feishu_bot", "offline", {"transport": "websocket"})


if __name__ == "__main__":
    main()
