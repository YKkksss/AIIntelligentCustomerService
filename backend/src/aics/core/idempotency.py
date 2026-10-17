"""写接口幂等键（设计讨论 0009 D9、接口规范 §7）。

Redis 键 `{prefix}idem:{key_id}:{Idempotency-Key}`，TTL 24 小时：

- 首次请求写占位 `{"state":"processing","sha":…}`；
- 处理中重复请求 → `409 CONFLICT`（首次仍在处理）；
- 已成功且请求体摘要相同 → 回放首次应答（状态码 + 响应体）；
- 同一幂等键配不同请求体 → `409 IDEMPOTENCY_CONFLICT`；
- 失败（抛异常 / 非 2xx）→ 删除占位，允许客户端重试；
- Redis 不可用一律**放行**并记 warning（与限流、`last_used_at` 一致；上传仍有 `content_hash` 去重兜底）。

用法：

```python
async with idem.lock(key_id=principal.key_id, idem=request.headers.get(HEADER), digest=d) as guard:
    if guard.replay is not None:
        return JSONResponse(guard.replay.body, status_code=guard.replay.status)
    result = await service.upload_file(...)
    await guard.record(202, body)
    return JSONResponse(body, status_code=202)
```
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from redis.exceptions import RedisError

from aics.core.errors import AppError

log = structlog.get_logger()

HEADER = "Idempotency-Key"
TTL_S = 86400
MAX_KEY_CHARS = 128
MAX_REPLAY_BYTES = 64 * 1024
PROCESSING = "processing"
DONE = "done"


class IdempotencyConflictError(AppError):
    """同一幂等键被用于不同的请求体（接口规范 §7）。"""

    code, status, message = "IDEMPOTENCY_CONFLICT", 409, "同一幂等键已用于不同的请求体"


class IdempotencyBusyError(AppError):
    """相同请求仍在处理中（接口规范 §7）。"""

    code, status, message = "CONFLICT", 409, "相同请求正在处理中，请稍后重试"


def canonical_json(value: Any) -> str:
    """请求体的规范 JSON：键排序、无多余空白，用于计算摘要。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_upload(data: bytes, fields: dict[str, Any] | None = None) -> str:
    """multipart 请求体摘要：文件 SHA-256 + 其它字段的规范 JSON。"""
    return digest_json({"file": digest_bytes(data), "fields": fields or {}})


@dataclass(frozen=True)
class Replay:
    """首次成功的应答，可直接复用。"""

    status: int
    body: dict[str, Any]


class IdempotencyGuard:
    """`async with idempotency.lock(...) as guard` 的句柄。"""

    def __init__(self, redis: Any, key: str, digest: str, replay: Replay | None = None) -> None:
        self._redis = redis
        self._key = key
        self._digest = digest
        self._recorded = False
        self.replay = replay

    @property
    def recorded(self) -> bool:
        return self._recorded

    async def record(self, status: int, body: dict[str, Any]) -> None:
        """成功应答落库以便回放；无法序列化或过大就不缓存（占位随之删除）。"""
        if self._redis is None:
            self._recorded = True
            return
        try:
            payload = json.dumps(
                {"state": DONE, "sha": self._digest, "status": status, "body": body},
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            )
        except (TypeError, ValueError):
            log.warning("idempotency.body_unserializable")
            return
        if len(payload) > MAX_REPLAY_BYTES:
            log.warning("idempotency.body_too_large", size=len(payload))
            return
        self._recorded = True
        try:
            await self._redis.set(self._key, payload, ex=TTL_S)
        except RedisError:
            log.warning("idempotency.store_failed")
            await self._release()

    async def _release(self) -> None:
        """删除占位，让失败 / 未缓存的请求可以重试。"""
        if self._redis is None or not self._key:
            return
        try:
            await self._redis.delete(self._key)
        except RedisError:
            log.warning("idempotency.release_failed")


class Idempotency:
    """纯 Redis 的幂等键（每个进程一个实例即可）。"""

    def __init__(self, redis: Any = None, prefix: str = "aics:", ttl_s: int = TTL_S) -> None:
        self.redis = redis
        self.prefix = prefix
        self.ttl_s = ttl_s

    def cache_key(self, key_id: UUID, idem: str) -> str:
        return f"{self.prefix}idem:{key_id}:{idem.strip()[:MAX_KEY_CHARS]}"

    @asynccontextmanager
    async def lock(
        self, *, key_id: UUID, idem: str | None, digest: str
    ) -> AsyncIterator[IdempotencyGuard]:
        """占位并返回句柄；无幂等键、Redis 不可用时退化为「不做幂等」。"""
        if self.redis is None or not idem or not idem.strip():
            yield IdempotencyGuard(None, "", digest)
            return
        key = self.cache_key(key_id, idem)
        try:
            existing = await self.redis.get(key)
        except RedisError:
            log.warning("idempotency.unavailable")
            yield IdempotencyGuard(None, key, digest)
            return
        if existing is not None:
            yield IdempotencyGuard(None, key, digest, self._replay_or_raise(existing, digest))
            return
        try:
            stored = await self.redis.set(
                key,
                json.dumps({"state": PROCESSING, "sha": digest}, separators=(",", ":")),
                nx=True,
                ex=self.ttl_s,
            )
        except RedisError:
            log.warning("idempotency.unavailable")
            yield IdempotencyGuard(None, key, digest)
            return
        if not stored:
            # 并发请求刚写下占位：按它当前的状态决定回放 / 冲突。
            try:
                again = await self.redis.get(key)
            except RedisError:
                yield IdempotencyGuard(None, key, digest)
                return
            if again is None:
                yield IdempotencyGuard(None, key, digest)
                return
            yield IdempotencyGuard(None, key, digest, self._replay_or_raise(again, digest))
            return
        guard = IdempotencyGuard(self.redis, key, digest)
        try:
            yield guard
        finally:
            if not guard.recorded:
                await guard._release()

    def _replay_or_raise(self, raw: Any, digest: str) -> Replay:
        record = _parse(raw)
        if record is None:
            log.warning("idempotency.placeholder_unreadable")
            raise IdempotencyBusyError(details={"reason": "placeholder_unreadable"})
        if record.get("sha") != digest:
            raise IdempotencyConflictError(details={"reason": "body_mismatch"})
        if record.get("state") == PROCESSING:
            raise IdempotencyBusyError()
        status, body = record.get("status"), record.get("body")
        if not isinstance(status, int) or not isinstance(body, dict):
            log.warning("idempotency.placeholder_unreadable")
            raise IdempotencyBusyError(details={"reason": "placeholder_unreadable"})
        log.info("idempotency.replayed", status=status)
        return Replay(status=status, body=body)


def _parse(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, bytes):  # pragma: no cover - FakeRedis 存字符串
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
