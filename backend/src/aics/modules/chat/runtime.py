"""Run-time dependencies of chat.service (built by `aics.main`, injected; 模块划分 §4).

chat may not import knowledge / apikeys (import-linter), so the composition root (`aics.main`,
or a test / eval script) builds the retrieval ports and hands them over here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.memory.service import MemoryService
from aics.modules.chat.memory_port import MemoryPortAdapter
from aics.rag.ports import ChunkSearch, RagPorts
from aics.rag.retrieval import VectorSearch

SHUTDOWN_GRACE_S = 30.0  # in-flight turns get this long to persist on shutdown


@dataclass
class ChatRuntime:
    sessions: async_sessionmaker[AsyncSession]
    chunk_search: ChunkSearch
    llm: Any  # providers.LLMProvider
    memory: MemoryService
    vector: VectorSearch | None = None
    reranker: Any = None  # providers.RerankProvider
    # Turn tasks still running (they persist the assistant message even if the client left).
    inflight: set[asyncio.Task[Any]] = field(default_factory=set)

    def ports(self) -> RagPorts:
        return RagPorts(
            chunk_search=self.chunk_search,
            vector=self.vector,
            reranker=self.reranker,
            llm=self.llm,
            memory=MemoryPortAdapter(self.memory),
        )

    async def drain(self, timeout_s: float = SHUTDOWN_GRACE_S) -> None:
        """Wait for in-flight turns (lifespan shutdown / tests)."""
        if self.inflight:
            await asyncio.wait(set(self.inflight), timeout=timeout_s)
