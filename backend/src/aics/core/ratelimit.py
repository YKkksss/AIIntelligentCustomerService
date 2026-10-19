"""固定窗口限流（接口规范 §6）：登录按 IP 10 次 / 分钟，与 API Key 的按分钟桶同一个思路。

从 `modules.apikeys.ratelimit` 抽出来放进 `core`：`modules.auth` 是独立模块，不能 import
`modules.apikeys`（ADR-0017 的 independence 合同），而限流实现与鉴权无关，属于横切能力。
API Key 那套还多一个"按 Shanghai 自然日"的配额，留在 modules 里，只复用这里错误类与固定窗口。

Redis 出错时**放行**（记日志 + 指标）：限流保护容量，不是鉴权边界。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog
from redis.exceptions import RedisError

from aics.core import metrics
from aics.core.errors import AppError

log = structlog.get_logger()


class RateLimitedError(AppError):
    code, status, message = "RATE_LIMITED", 429, "请求过于频繁，请稍后再试"


@dataclass(frozen=True)
class Limits:
    """One fixed-window budget; `key` is the Redis key prefix of the subject."""

    key: str
    limit: int
    window_s: int = 60


class FixedWindowLimiter:
    def __init__(self, redis: Any, *, prefix: str = "aics:") -> None:
        self.redis = redis
        self.prefix = prefix

    async def hit(self, limits: Limits, *, now: float | None = None) -> dict[str, str]:
        """Count one request; returns `X-RateLimit-*` headers or raises 429."""
        ts = time.time() if now is None else now
        window = int(ts // limits.window_s)
        reset = (window + 1) * limits.window_s
        try:
            count = await self._incr(f"{self.prefix}rl:{limits.key}:{window}", limits.window_s * 2)
        except RedisError as e:  # fail open: capacity protection, not an auth boundary
            log.warning("ratelimit.unavailable", error_type=type(e).__name__)
            metrics.inc("aics_ratelimit_errors_total")
            return {}
        headers = {
            "X-RateLimit-Limit": str(limits.limit),
            "X-RateLimit-Remaining": str(max(0, limits.limit - count)),
            "X-RateLimit-Reset": str(reset),
        }
        if count > limits.limit:
            metrics.inc("aics_ratelimit_rejected_total", reason="login")
            raise RateLimitedError(headers={**headers, "Retry-After": str(max(1, reset - int(ts)))})
        return headers

    async def _incr(self, key: str, ttl: int) -> int:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl)
            count, _ = await pipe.execute()
        return int(count)
