"""API Key usage tracking (M3.1; 接口规范 §2, 数据库设计 §3.5).

A successful authentication writes one field of the current UTC minute bucket
`{prefix}keyuse:{yyyymmddHHMM}` (`HSET key_id unix_ts`, TTL `KEYUSE_BUCKET_TTL_S`) instead of
touching `api_keys.last_used_at`, so a busy key never costs a database write. The beat task
`aics.flush_key_usage` reads the buckets of the last `KEYUSE_WINDOW` minutes (plus the current
one), keeps the newest timestamp per key, deletes the buckets of past minutes and applies one
`UPDATE api_keys SET last_used_at = greatest(last_used_at, :ts)`, so late or out-of-order buckets
can never move the timestamp backwards.

Buckets are only ever written into the bucket of the current minute, so the bucket of a past
minute is immutable and safe to delete — no Lua script and no HGETALL/HDEL race. Redis failures
never fail an authenticated request (logged only, like the rate limiter); a missed flush is
recovered by the next run while the bucket is alive, so the timestamp is best-effort by design.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from redis.exceptions import RedisError
from sqlalchemy import case, func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.db.models import ApiKey

log = structlog.get_logger()

KEYUSE_WINDOW = 10  # past minutes read (and swept) by the flusher, plus the current one
KEYUSE_BUCKET_TTL_S = 900  # 15 min: window + slack for a stalled beat
KEY_USAGE_FLUSH_TASK = "aics.flush_key_usage"
KEY_USAGE_FLUSH_INTERVAL_S = (
    60.0  # = tasks.celery_app.KEY_USAGE_FLUSH_INTERVAL_S (tests keep them equal)
)


def _ts(now: float | None) -> float:
    return time.time() if now is None else now


def bucket_key(prefix: str, now: float | None = None) -> str:
    """Redis key of the UTC minute bucket containing `now`."""
    return f"{prefix}keyuse:{datetime.fromtimestamp(_ts(now), UTC):%Y%m%d%H%M}"


async def record_use(redis: Any, prefix: str, key_id: UUID, *, now: float | None = None) -> None:
    """Best-effort `last_used_at` marker: one HSET + EXPIRE, never raises."""
    ts = int(_ts(now))
    key = f"{prefix}keyuse:{datetime.fromtimestamp(ts, UTC):%Y%m%d%H%M}"
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.hset(key, str(key_id), str(ts))
            pipe.expire(key, KEYUSE_BUCKET_TTL_S)
            await pipe.execute()
    except RedisError as e:
        log.warning("apikeys.key_usage_unavailable", error_type=type(e).__name__)


def _text(raw: Any) -> str:
    """Redis replies are bytes unless the client sets `decode_responses` (ours does not)."""
    return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)


def merge_buckets(buckets: Iterable[Mapping[str, Any]]) -> dict[UUID, int]:
    """`{key_id: unix_ts}` keeping the newest timestamp; unparsable entries are dropped."""
    newest: dict[UUID, int] = {}
    for bucket in buckets:
        for raw_id, raw_ts in bucket.items():
            try:
                key_id, ts = UUID(_text(raw_id)), int(_text(raw_ts))
            except (ValueError, TypeError):
                continue
            if ts > newest.get(key_id, 0):
                newest[key_id] = ts
    return newest


async def flush_key_usage(
    sessions: async_sessionmaker[AsyncSession],
    redis: Any,
    *,
    prefix: str = "aics:",
    now: float | None = None,
    window: int = KEYUSE_WINDOW,
) -> dict[str, int]:
    """Fold the recent buckets into `api_keys.last_used_at`; returns key / bucket counters."""
    ts = _ts(now)
    read: list[dict[str, Any]] = []
    buckets = 0
    for minute in range(window + 1):  # minute 0 is still being written: read it, keep it
        key = bucket_key(prefix, ts - minute * 60)
        try:
            raw = await redis.hgetall(key)
        except RedisError as e:
            log.warning("apikeys.key_usage_read_failed", error_type=type(e).__name__)
            return {"keys": 0, "buckets": 0}
        if not raw:
            continue
        buckets += 1
        read.append(raw)
        if minute:  # past minutes are immutable → safe to drop
            with contextlib.suppress(RedisError):
                await redis.delete(key)
    newest = merge_buckets(read)
    if not newest:
        return {"keys": 0, "buckets": buckets}
    # One statement: PostgreSQL GREATEST ignores NULLs, so a key used for the first time is set.
    stamps = case({k: func.to_timestamp(v) for k, v in newest.items()}, value=ApiKey.id)
    stmt = (
        update(ApiKey)
        .where(ApiKey.id.in_(list(newest)))
        .values(last_used_at=func.greatest(ApiKey.last_used_at, stamps))
    )
    async with sessions() as s:
        await s.execute(stmt)
        await s.commit()
    log.info("apikeys.key_usage_flushed", keys=len(newest), buckets=buckets)
    return {"keys": len(newest), "buckets": buckets}


async def run_key_usage_flush_job(settings: Any = None) -> dict[str, int]:
    """Body of the Celery task: a NullPool engine and its own Redis client per run, because each
    task runs in its own event loop (asyncio.run)."""
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from aics.core.config import get_settings

    s = settings or get_settings()
    engine = create_async_engine(s.db_url, poolclass=NullPool)
    redis = aioredis.from_url(s.redis_url)
    try:
        return await flush_key_usage(
            async_sessionmaker(engine, expire_on_commit=False), redis, prefix=s.redis_prefix
        )
    finally:
        await redis.aclose()
        await engine.dispose()
