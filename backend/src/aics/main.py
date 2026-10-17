"""FastAPI app factory 与 `aics-api` 入口。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
import structlog
import uvicorn
from celery import Celery
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from aics import __version__
from aics.core.config import Settings, get_settings
from aics.core.errors import AppError, error_body
from aics.core.idempotency import Idempotency
from aics.core.logging import RequestLogMiddleware, configure_logging
from aics.db.engine import dispose_engine, get_sessionmaker
from aics.health import router as health_router
from aics.memory.compactor import CompactParams
from aics.memory.service import COMPACT_TASK, MemoryService
from aics.memory.units import pg_units
from aics.modules.apikeys.service import ApiKeyAuthenticator
from aics.modules.chat.router import router as chat_router
from aics.modules.chat.runtime import ChatRuntime
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.modules.knowledge.router import router as knowledge_router
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.knowledge.service import TASK_QUEUES
from aics.providers import get_embedder, get_llm, get_reranker
from aics.rag.retrieval import EmbeddingVectorSearch
from aics.storage import build_storage
from aics.vectorstore import get_vector_store

log = structlog.get_logger()


def build_chat_runtime(s: Settings) -> ChatRuntime:
    """Composition root of chat.service: PG keyword search, Qdrant vectors, rerank, LLM and
    memory (online compaction is enqueued to the `context` queue by task name)."""
    sessions = get_sessionmaker()
    broker = Celery("aics-api", broker=s.celery_broker_url)

    def send(app_id: UUID, conversation_id: UUID, version: int, params: CompactParams) -> None:
        try:
            broker.send_task(
                COMPACT_TASK,
                args=[str(app_id), str(conversation_id), version, params.model_dump()],
                queue="context",
                routing_key="context",
                exchange="aics",
                retry=True,
                retry_policy={"max_retries": 2, "interval_start": 0.2, "interval_step": 0.5},
            )
        except Exception as e:  # compaction is best-effort; the next turn re-checks
            log.warning("chat.compact_enqueue_failed", error_type=type(e).__name__)

    def enqueue(app_id: UUID, conversation_id: UUID, version: int, params: CompactParams) -> None:
        # Publishing is blocking network I/O: keep it off the event loop.
        asyncio.get_running_loop().run_in_executor(
            None, send, app_id, conversation_id, version, params
        )

    return ChatRuntime(
        sessions=sessions,
        chunk_search=PgChunkSearch(sessions),
        vector=EmbeddingVectorSearch(get_embedder(), get_vector_store()),
        reranker=get_reranker(),
        llm=get_llm(),
        memory=MemoryService(pg_units(sessions), enqueue_compaction=enqueue),
    )


def build_knowledge_runtime(s: Settings) -> KnowledgeRuntime:
    """Composition root of knowledge.service: 本地存储、Qdrant、嵌入器与任务投递。

    入队按 `TASK_QUEUES` 指定队列（与 `tasks.celery_app.task_routes` 一致）；投递失败只记日志，
    文档停在 queued/parsing，由 `aics.sweep_knowledge` 兜底（流水线 §7）。
    """
    sessions = get_sessionmaker()
    broker = Celery("aics-api", broker=s.celery_broker_url)

    def send(task: str, args: list[Any]) -> None:
        queue = TASK_QUEUES.get(task, "maintenance")
        try:
            broker.send_task(
                task,
                args=args,
                queue=queue,
                routing_key=queue,
                exchange="aics",
                retry=True,
                retry_policy={"max_retries": 2, "interval_start": 0.2, "interval_step": 0.5},
            )
        except Exception as e:  # noqa: BLE001 - 投递失败不阻断请求，sweep 会补
            log.warning("knowledge.enqueue_failed", task=task, error_type=type(e).__name__)

    def enqueue(task: str, args: Sequence[Any]) -> None:
        # Publishing is blocking network I/O: keep it off the event loop.
        asyncio.get_running_loop().run_in_executor(None, send, task, list(args))

    return KnowledgeRuntime(
        sessions=sessions,
        storage=build_storage(s),
        embedder=get_embedder(),
        vector=get_vector_store(),
        enqueue=enqueue,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    redis = aioredis.from_url(s.redis_url)
    # Idempotency keys of the open write API (`core.idempotency`); Redis is also the
    # authenticator cache, so one client serves both.
    app.state.redis = redis
    app.state.idempotency = Idempotency(redis, prefix=s.redis_prefix)
    # Open-API authentication (core.security.Authenticator), implemented by modules.apikeys.
    app.state.authenticator = ApiKeyAuthenticator(
        get_sessionmaker(),
        pepper=s.api_key_pepper.get_secret_value(),
        redis=redis,
        redis_prefix=s.redis_prefix,
        trusted_proxies=s.trusted_proxies,
    )
    if getattr(app.state, "chat_runtime", None) is None:  # tests may install their own
        app.state.chat_runtime = build_chat_runtime(s)
    if getattr(app.state, "knowledge_runtime", None) is None:  # tests may install their own
        app.state.knowledge_runtime = build_knowledge_runtime(s)
    log.info("app.started", env=s.env, version=__version__)
    yield
    await app.state.chat_runtime.drain()
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
    app.include_router(chat_router)
    app.include_router(knowledge_router)
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
