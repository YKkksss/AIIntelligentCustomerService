"""Minimal in-memory async Redis for unit tests (get / set ex / delete / incr / expire /
pipeline). `fail=True` makes every call raise RedisError."""

from __future__ import annotations

from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError


class _Pipe:
    def __init__(self, r: FakeRedis) -> None:
        self.r = r
        self.ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def incr(self, key: str) -> None:
        self.ops.append(("incr", (key,)))

    def expire(self, key: str, ttl: int) -> None:
        self.ops.append(("expire", (key, ttl)))

    async def execute(self) -> list[Any]:
        return [await getattr(self.r, op)(*args) for op, args in self.ops]


class FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.data: dict[str, Any] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail

    def _check(self) -> None:
        if self.fail:
            raise RedisConnectionError("redis down")

    async def get(self, key: str) -> Any:
        self._check()
        return self.data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self._check()
        self.data[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    async def delete(self, *keys: str) -> int:
        self._check()
        return sum(self.data.pop(k, None) is not None for k in keys)

    async def incr(self, key: str) -> int:
        self._check()
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self._check()
        self.ttl[key] = ttl
        return True

    def pipeline(self, transaction: bool = True) -> _Pipe:
        self._check()
        return _Pipe(self)
