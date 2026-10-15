"""结构化日志（structlog）。

JSON 字段：ts, level, event, request_id, app_id, user_id|api_key_prefix, route, status, latency_ms
（docs/07-deployment/05-监控与运维手册.md §2）。事件名用 `模块.动作`。禁止记录密钥、密码、完整 API Key。
"""

from __future__ import annotations

import logging
import sys
import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False, key="ts"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    # 标准库日志（uvicorn、sqlalchemy、celery）统一输出级别
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s", force=True)
    logging.getLogger("uvicorn.access").disabled = True  # 由 RequestLogMiddleware 记录


def new_request_id() -> str:
    return uuid.uuid4().hex


class RequestLogMiddleware:
    """读取或生成 X-Request-ID，绑定到日志上下文并写回响应头；每个请求记一条 `http.request`。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        rid = headers.get(REQUEST_ID_HEADER) or new_request_id()
        rid = rid[:64]
        scope.setdefault("state", {})["request_id"] = rid
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((REQUEST_ID_HEADER.encode(), rid.encode()))
            await send(message)

        log = structlog.get_logger()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            log.info(
                "http.request",
                method=scope.get("method"),
                route=getattr(route, "path", scope.get("path")),
                status=status,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
            )
            structlog.contextvars.clear_contextvars()
