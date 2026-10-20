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
from starlette.exceptions import HTTPException as StarletteHTTPException

from aics import __version__
from aics.core.audit import AuditSink
from aics.core.config import Settings, get_settings
from aics.core.errors import AppError, error_body
from aics.core.idempotency import Idempotency
from aics.core.logging import RequestLogMiddleware, configure_logging
from aics.db.engine import dispose_engine, get_sessionmaker
from aics.health import router as health_router
from aics.memory.compactor import Compactor, CompactParams
from aics.memory.locks import RedisLock
from aics.memory.service import COMPACT_TASK, MemoryService
from aics.memory.units import pg_units
from aics.modules.apikeys.service import ApiKeyAuthenticator
from aics.modules.apps.admin_router import router as apps_admin_router
from aics.modules.apps.runtime import AppsAdminRuntime
from aics.modules.audit.admin_router import router as audit_router
from aics.modules.audit.runtime import AuditRuntime
from aics.modules.audit.sink import PgAuditSink
from aics.modules.auth.admin_router import router as auth_router
from aics.modules.auth.runtime import AuthRuntime
from aics.modules.chat import openai
from aics.modules.chat.admin_router import router as chat_admin_router
from aics.modules.chat.openai_router import router as openai_router
from aics.modules.chat.router import router as chat_router
from aics.modules.chat.runtime import ChatRuntime
from aics.modules.feedback.router import router as feedback_router
from aics.modules.feedback.runtime import FeedbackRuntime
from aics.modules.handoff.router import router as handoff_router
from aics.modules.handoff.runtime import HandoffRuntime
from aics.modules.knowledge.admin_router import router as knowledge_admin_router
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.modules.knowledge.router import router as knowledge_router
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.knowledge.service import TASK_QUEUES
from aics.providers import get_embedder, get_llm, get_reranker
from aics.rag.retrieval import EmbeddingVectorSearch
from aics.storage import build_storage
from aics.vectorstore import get_vector_store

log = structlog.get_logger()


def build_chat_runtime(s: Settings, *, redis: Any | None = None) -> ChatRuntime:
    """Composition root of chat.service: PG keyword search, Qdrant vectors, rerank, LLM and
    memory (online compaction is enqueued to the `context` queue by task name).

    `redis` 同时供记忆压缩锁与临时摘要使用（OpenAI 兼容的无状态请求用临时摘要压缩请求内历史，
    M3.3）；不传 Redis 时退化为纯裁剪，`openai_temp_summary` 不生效。"""
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

    compactor = (
        Compactor(pg_units(sessions), get_llm(), RedisLock(redis, s.redis_prefix))
        if redis is not None
        else None
    )
    return ChatRuntime(
        sessions=sessions,
        chunk_search=PgChunkSearch(sessions),
        vector=EmbeddingVectorSearch(get_embedder(), get_vector_store()),
        reranker=get_reranker(),
        llm=get_llm(),
        memory=MemoryService(pg_units(sessions), compactor=compactor, enqueue_compaction=enqueue),
        redis=redis,
        redis_prefix=s.redis_prefix,
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
        reranker=get_reranker(),
        audit=PgAuditSink(),
        enqueue=enqueue,
    )


def build_handoff_runtime(s: Settings, *, redis: Any | None = None) -> HandoffRuntime:
    """Composition root of `modules.handoff`: 坐席接口只需要会话工厂（M3.4 ②）。

    转人工状态迁移只写 `conversations` / `messages`，模型与向量库都不参与；`redis` 供坐席动作
    提交后广播会话事件（M3.5 ②），不传时事件只落库。
    """
    return HandoffRuntime(sessions=get_sessionmaker(), redis=redis, redis_prefix=s.redis_prefix)


def build_feedback_runtime(s: Settings) -> FeedbackRuntime:
    """Composition root of `modules.feedback`: 只写 `feedback` 一张表（M3.6 ②）。

    `s` 目前没有用到的字段，签名保留是为了与其它 `build_*_runtime` 一致，将来接评测用例
    （M5.3）时可以在这里塞依赖而不用改调用点。
    """
    return FeedbackRuntime(sessions=get_sessionmaker())


def build_audit_runtime(s: Settings) -> AuditRuntime:
    """Composition root of `modules.audit`（M4.1 ③）：审计写入端与查询。

    `PgAuditSink` 由 `build_auth_runtime(audit=...)` 注入认证服务，认证类动作因此在业务事务里落库；
    `GET /api/v1/admin/audit` 用同一份 sessionmaker 读。
    """
    return AuditRuntime(sessions=get_sessionmaker())


def build_auth_runtime(
    s: Settings, *, redis: Any | None = None, audit: AuditSink | None = None
) -> AuthRuntime:
    """Composition root of `modules.auth`（M4.1 ②）：JWT 认证 + RBAC。

    `redis` 同时用于刷新令牌轮换（`{prefix}auth:refresh:*`）与鉴权缓存（60 秒）；不传时
    退化成「不轮换、每次都回表」，本地单测照样跑得动，生产必须配。
    """
    return AuthRuntime(sessions=get_sessionmaker(), settings=s, redis=redis, audit=audit)


def build_apps_admin_runtime(
    s: Settings, *, audit: AuditSink | None = None, redis: Any | None = None
) -> AppsAdminRuntime:
    """Composition root of `modules.apps` 的管理接口（M4.2）。

    `apps` 是独立模块：审计端口由这里注入（`modules.audit`），任务投递用与
    `build_knowledge_runtime` 相同的 broker 直发模式（`aics.retokenize_app` 在 ingest 队列）。
    `storage` / `vector` 留给 ③ 的删除任务。
    """
    broker = Celery("aics-api", broker=s.celery_broker_url)

    def send(task: str, args: Sequence[Any]) -> None:
        queue = TASK_QUEUES.get(task, "maintenance")
        try:
            broker.send_task(
                task,
                args=list(args),
                queue=queue,
                routing_key=queue,
                exchange="aics",
                retry=True,
                retry_policy={"max_retries": 2, "interval_start": 0.2, "interval_step": 0.5},
            )
        except Exception as e:  # noqa: BLE001 - 投递失败不阻断请求，任务行留待 sweep
            log.warning("apps.enqueue_failed", task=task, error_type=type(e).__name__)

    def enqueue(task: str, args: Sequence[Any]) -> None:
        asyncio.get_running_loop().run_in_executor(None, send, task, list(args))

    return AppsAdminRuntime(
        sessions=get_sessionmaker(),
        settings=s,
        audit=audit,
        enqueue=enqueue,
        storage=build_storage(s),
        vector=get_vector_store(),
        redis=redis,
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
        app.state.chat_runtime = build_chat_runtime(s, redis=redis)
    if getattr(app.state, "knowledge_runtime", None) is None:  # tests may install their own
        app.state.knowledge_runtime = build_knowledge_runtime(s)
    if getattr(app.state, "handoff_runtime", None) is None:  # tests may install their own
        app.state.handoff_runtime = build_handoff_runtime(s, redis=redis)
    if getattr(app.state, "feedback_runtime", None) is None:  # tests may install their own
        app.state.feedback_runtime = build_feedback_runtime(s)
    if getattr(app.state, "audit_runtime", None) is None:  # tests may install their own
        app.state.audit_runtime = build_audit_runtime(s)
    if getattr(app.state, "auth_runtime", None) is None:  # tests may install their own
        app.state.auth_runtime = build_auth_runtime(
            s, redis=redis, audit=app.state.audit_runtime.sink()
        )
    if getattr(app.state, "authorizer", None) is None:  # core.security.Authorizer (RBAC)
        app.state.authorizer = app.state.auth_runtime.authorizer()
    if getattr(app.state, "apps_admin_runtime", None) is None:  # tests may install their own
        app.state.apps_admin_runtime = build_apps_admin_runtime(
            s, audit=app.state.audit_runtime.sink(), redis=redis
        )
    log.info("app.started", env=s.env, version=__version__)
    yield
    await app.state.chat_runtime.drain()
    await redis.aclose()
    await dispose_engine()
    log.info("app.stopped")


def _rid(request: Request) -> str | None:
    return request.scope.get("state", {}).get("request_id")


def _openai_path(request: Request) -> bool:
    """`/v1/*` 的鉴权与应答都是 OpenAI 形状（开放接口参考 §3），错误体也要跟着换。"""
    return request.url.path.startswith("/v1/")


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
        body = (
            openai.error_body(exc.code, exc.message, exc.status)
            if _openai_path(request)
            else error_body(exc.code, exc.message, _rid(request), exc.details)
        )
        return JSONResponse(
            body,
            status_code=exc.status,
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = {"errors": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]}
        if _openai_path(request):
            return JSONResponse(
                openai.error_body("INVALID_REQUEST", "参数校验失败", 422), status_code=422
            )
        return JSONResponse(
            error_body("VALIDATION_ERROR", "参数校验失败", _rid(request), details),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Starlette 自己的 404 / 405 也走统一错误体（ISSUE-01，M3.6 ⑤）。"""
        if exc.status_code == 404:
            code, message = "NOT_FOUND", "资源不存在"
        elif exc.status_code == 405:
            code, message = "METHOD_NOT_ALLOWED", "方法不允许"
        else:
            # 其余状态交给 Starlette 原样返回（错误码表里没有对应项，不硬造）。
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        body = (
            openai.error_body(code, message, exc.status_code)
            if _openai_path(request)
            else error_body(code, message, _rid(request))
        )
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers or None)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("app.unhandled_error", error_type=type(exc).__name__)
        body = (
            openai.error_body("INTERNAL_ERROR", "服务内部错误", 500)
            if _openai_path(request)
            else error_body("INTERNAL_ERROR", "服务内部错误", _rid(request))
        )
        return JSONResponse(body, status_code=500)

    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(knowledge_router)
    app.include_router(knowledge_admin_router)
    app.include_router(handoff_router)
    app.include_router(feedback_router)
    app.include_router(auth_router)
    app.include_router(audit_router)
    app.include_router(apps_admin_router)
    app.include_router(chat_admin_router)
    app.include_router(openai_router)
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
