"""Verified HTTPS ingress for Feishu interactive-card callbacks."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from app.integrations.feishu.gateway import CardActionEvent, FeishuCardActionAdapter


logger = logging.getLogger(__name__)


class FeishuCardCallbackServer:
    """A small ASGI server that validates and commits card actions synchronously."""

    def __init__(
        self,
        *,
        app_id: str,
        verification_token: str,
        encrypt_key: str,
        action_handler: Callable[[CardActionEvent], dict[str, Any]],
        host: str,
        port: int,
        path: str,
    ):
        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import PlainTextResponse, Response
        from starlette.routing import Route

        self.action_handler = action_handler
        self.host = host
        self.port = int(port)
        self.path = path
        self.adapter = FeishuCardActionAdapter(app_id)
        self._server: Any | None = None
        self._task: asyncio.Task[None] | None = None

        def on_card_action(callback: Any) -> Any:
            try:
                event = self.adapter.adapt_p2(callback)
                result = self.action_handler(event)
                if not result.get("ok"):
                    raise ValueError("unsupported card action")
                content = str(result.get("reply_text") or "提交成功")
            except (ValueError, RuntimeError):
                logger.warning("feishu_card_callback_rejected", exc_info=True)
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": "提交失败，请稍后重试"}}
                )
            return P2CardActionTriggerResponse(
                {"toast": {"type": "success", "content": content}}
            )

        self.dispatcher = (
            lark.EventDispatcherHandler.builder(
                encrypt_key, verification_token, lark.LogLevel.WARNING
            )
            .register_p2_card_action_trigger(on_card_action)
            .build()
        )

        async def callback_endpoint(request: Request) -> Response:
            from lark_oapi.core.model.raw_request import RawRequest

            raw = RawRequest()
            raw.uri = str(request.url.path)
            raw.headers = dict(request.headers)
            # Starlette normalizes incoming header names to lower case, while
            # the official SDK looks up Feishu's canonical header spelling.
            for name in (
                "X-Lark-Request-Timestamp",
                "X-Lark-Request-Nonce",
                "X-Lark-Signature",
                "X-Lark-Request-Id",
            ):
                value = request.headers.get(name)
                if value is not None:
                    raw.headers[name] = value
            raw.body = await request.body()
            response = await asyncio.to_thread(self.dispatcher.do, raw)
            content = getattr(response, "content", b"") or b""
            if isinstance(content, str):
                content = content.encode("utf-8")
            return Response(
                content=content,
                status_code=int(getattr(response, "status_code", 200) or 200),
                media_type="application/json",
            )

        async def health(_request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        self.app = Starlette(
            routes=[
                Route(path, callback_endpoint, methods=["POST"]),
                Route("/healthz", health, methods=["GET"]),
            ]
        )

    async def start(self, *, timeout_seconds: float = 10.0) -> None:
        if self._task is not None:
            raise RuntimeError("Feishu card callback server is already started")
        import uvicorn

        loop = asyncio.get_running_loop()
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(
            self._server.serve(), name="feishu-card-callback-server"
        )
        deadline = loop.time() + timeout_seconds
        while not self._server.started:
            if self._task.done():
                await self._task
                raise RuntimeError("Feishu card callback server stopped during startup")
            if loop.time() >= deadline:
                await self.stop()
                raise TimeoutError("Feishu card callback server startup timed out")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        task = self._task
        server = self._server
        if task is None:
            return
        if server is not None:
            server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._server = None
