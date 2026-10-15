"""存活与就绪检查：/healthz、/readyz（docs/04-api/02-开放接口参考.md）。

/readyz 并发检查 PG、Redis、RabbitMQ、Qdrant，每项独立超时；任一失败返回 503。
失败原因只写日志，不在响应中暴露连接串等细节。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from kombu import Connection
from sqlalchemy import text

from aics.core.config import get_settings
from aics.db.engine import get_engine

CHECK_TIMEOUT_S = 2.0

log = structlog.get_logger()
router = APIRouter(tags=["health"])

Check = Callable[[], Awaitable[None]]


async def check_postgres() -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def check_redis() -> None:
    client = aioredis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=CHECK_TIMEOUT_S,
        socket_timeout=CHECK_TIMEOUT_S,
    )
    try:
        await client.ping()
    finally:
        await client.aclose()


def _amqp_connect() -> None:
    with Connection(get_settings().celery_broker_url, connect_timeout=CHECK_TIMEOUT_S) as conn:
        conn.ensure_connection(max_retries=1, timeout=CHECK_TIMEOUT_S)


async def check_rabbitmq() -> None:
    await asyncio.to_thread(_amqp_connect)


async def check_qdrant() -> None:
    s = get_settings()
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S) as client:
        resp = await client.get(
            s.qdrant_url.rstrip("/") + "/readyz",
            headers={"api-key": s.qdrant_api_key.get_secret_value()},
        )
        resp.raise_for_status()


CHECKS: dict[str, Check] = {
    "postgres": check_postgres,
    "redis": check_redis,
    "rabbitmq": check_rabbitmq,
    "qdrant": check_qdrant,
}


async def _run(name: str, fn: Check, limit_s: float) -> tuple[str, str]:
    try:
        await asyncio.wait_for(fn(), timeout=limit_s)
        return name, "ok"
    except Exception as e:  # 任何失败都只算该依赖不可用
        log.warning("health.check_failed", dependency=name, error_type=type(e).__name__)
        return name, "error"


async def run_checks(
    checks: dict[str, Check] | None = None, limit_s: float = CHECK_TIMEOUT_S + 0.5
) -> dict[str, str]:
    checks = CHECKS if checks is None else checks
    results = await asyncio.gather(*(_run(n, f, limit_s) for n, f in checks.items()))
    return dict(results)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    results = await run_checks()
    ok = all(v == "ok" for v in results.values())
    return JSONResponse(
        {"status": "ok" if ok else "error", "checks": results}, status_code=200 if ok else 503
    )
