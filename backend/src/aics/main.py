"""FastAPI app factory 与 `aics-api` 入口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from aics import __version__
from aics.core.config import get_settings
from aics.core.errors import AppError, error_body
from aics.core.logging import RequestLogMiddleware, configure_logging
from aics.db.engine import dispose_engine, get_sessionmaker
from aics.health import router as health_router
from aics.modules.apikeys.service import ApiKeyAuthenticator

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    redis = aioredis.from_url(s.redis_url)
    # Open-API authentication (core.security.Authenticator), implemented by modules.apikeys.
    app.state.authenticator = ApiKeyAuthenticator(
        get_sessionmaker(),
        pepper=s.api_key_pepper.get_secret_value(),
        redis=redis,
        redis_prefix=s.redis_prefix,
    )
    log.info("app.started", env=s.env, version=__version__)
    yield
    await redis.aclose()
    await dispose_engine()
    log.info("app.stopped")


def _rid(request: Request) -> str | None:
    return request.scope.get("state", {}).get("request_id")


def create_app() -> FastAPI:
    s = get_settings()  # 配置校验失败在这里直接抛出，进程退出
    # prod 强制 JSON 日志
    configure_logging(s.log_level, json=s.log_json or s.is_prod)
    app = FastAPI(
        title="AICS",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if s.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if s.is_prod else "/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestLogMiddleware)  # 最外层：CORS 预检也有 request_id

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, _rid(request), exc.details),
            status_code=exc.status,
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = {"errors": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]}
        return JSONResponse(
            error_body("VALIDATION_ERROR", "参数校验失败", _rid(request), details),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("app.unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            error_body("INTERNAL_ERROR", "服务内部错误", _rid(request)), status_code=500
        )

    app.include_router(health_router)
    return app


def run() -> None:
    """`uv run aics-api`：开发环境启动（dev 下自动 reload）。"""
    s = get_settings()
    uvicorn.run(
        "aics.main:create_app",
        factory=True,
        host=s.api_host,
        port=s.api_port,
        reload=s.env == "dev",
        log_config=None,
        access_log=False,
    )
