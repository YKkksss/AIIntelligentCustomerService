"""Per-conversation compaction lock (对话上下文管理 §7.2).

`RedisLock`: `SET {prefix}lock:compact:{conversation_id} <token> NX EX 120`; released only by
its owner (compare-and-delete in Lua), so a task that outlived the TTL cannot delete the lock
of the next holder. `LocalLock` is the in-process equivalent for eval / tests without Redis.
The lock only avoids duplicate LLM work; correctness comes from `summary_version`.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any, Protocol
from uuid import UUID

COMPACT_LOCK_TTL_S = 120

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class CompactLock(Protocol):
    async def acquire(self, conversation_id: UUID) -> str | None:
        """Owner token, or None if another holder has it."""
        ...

    async def release(self, conversation_id: UUID, token: str) -> None: ...


class RedisLock:
    def __init__(self, client: Any, prefix: str = "aics:", ttl_s: int = COMPACT_LOCK_TTL_S):
        self.client = client
        self.prefix = prefix
        self.ttl_s = ttl_s

    def key(self, conversation_id: UUID) -> str:
        return f"{self.prefix}lock:compact:{conversation_id}"

    async def acquire(self, conversation_id: UUID) -> str | None:
        token = secrets.token_hex(16)
        ok = await self.client.set(self.key(conversation_id), token, nx=True, ex=self.ttl_s)
        return token if ok else None

    async def release(self, conversation_id: UUID, token: str) -> None:
        await self.client.eval(_RELEASE_LUA, 1, self.key(conversation_id), token)


class LocalLock:
    """Single-process lock (eval sync path, unit tests)."""

    def __init__(self) -> None:
        self._held: dict[UUID, str] = {}
        self._mu = asyncio.Lock()

    async def acquire(self, conversation_id: UUID) -> str | None:
        async with self._mu:
            if conversation_id in self._held:
                return None
            token = secrets.token_hex(8)
            self._held[conversation_id] = token
            return token

    async def release(self, conversation_id: UUID, token: str) -> None:
        async with self._mu:
            if self._held.get(conversation_id) == token:
                del self._held[conversation_id]
