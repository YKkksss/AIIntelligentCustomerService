"""Per-key request limits (接口规范 §6).

- `rpm_limit`: fixed one-minute window, Redis key `{prefix}rl:{key_id}:{epoch_minute}`
  (INCR + EXPIRE 120 s in one pipeline);
- `daily_quota`: `{prefix}quota:{key_id}:{yyyymmdd}` (Asia/Shanghai calendar day, EXPIRE 2 d);
  only requests that passed the minute window are counted.
Over the limit → 429 `RATE_LIMITED` / `QUOTA_EXCEEDED` with `Retry-After` and
`X-RateLimit-*`. Redis errors fail open (logged + `aics_ratelimit_errors_total`): limits protect
capacity, they are not an authentication boundary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from redis.exceptions import RedisError

from aics.core import metrics
from aics.core.errors import AppError
from aics.core.ratelimit import RateLimitedError

log = structlog.get_logger()

QUOTA_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_TTL_S = 120
DAY_TTL_S = 2 * 86400


class QuotaExceededError(AppError):
    code, status, message = "QUOTA_EXCEEDED", 429, "今日调用次数已用完"


@dataclass(frozen=True)
class Limits:
    key_id: UUID
    rpm_limit: int
    daily_quota: int | None


class RateLimiter:
    def __init__(self, redis: Any, *, prefix: str = "aics:") -> None:
        self.redis = redis
        self.prefix = prefix

    async def hit(self, limits: Limits, *, now: float | None = None) -> dict[str, str]:
        """Count one request; returns X-RateLimit-* headers or raises 429."""
        ts = time.time() if now is None else now
        minute = int(ts // 60)
        reset = (minute + 1) * 60
        try:
            count = await self._incr(f"{self.prefix}rl:{limits.key_id}:{minute}", MINUTE_TTL_S)
        except RedisError as e:
            return self._fail_open(e)
        headers = {
            "X-RateLimit-Limit": str(limits.rpm_limit),
            "X-RateLimit-Remaining": str(max(0, limits.rpm_limit - count)),
            "X-RateLimit-Reset": str(reset),
        }
        if count > limits.rpm_limit:
            metrics.inc("aics_ratelimit_rejected_total", reason="rpm")
            retry = max(1, reset - int(ts))
            raise RateLimitedError(headers={**headers, "Retry-After": str(retry)})
        if limits.daily_quota is not None:
            day = datetime.fromtimestamp(ts, QUOTA_TZ)
            try:
                used = await self._incr(
                    f"{self.prefix}quota:{limits.key_id}:{day:%Y%m%d}", DAY_TTL_S
                )
            except RedisError as e:
                return self._fail_open(e, headers)
            if used > limits.daily_quota:
                metrics.inc("aics_ratelimit_rejected_total", reason="quota")
                midnight = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0)
                retry = max(1, int(midnight.timestamp() - ts))
                raise QuotaExceededError(headers={**headers, "Retry-After": str(retry)})
        return headers

    async def _incr(self, key: str, ttl: int) -> int:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl)
            count, _ = await pipe.execute()
        return int(count)

    @staticmethod
    def _fail_open(e: Exception, headers: dict[str, str] | None = None) -> dict[str, str]:
        log.warning("apikeys.ratelimit_unavailable", error_type=type(e).__name__)
        metrics.inc("aics_ratelimit_errors_total")
        return headers or {}
