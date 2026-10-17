"""knowledge 运行时：把开放接口需要的依赖（会话、存储、嵌入器、Qdrant、入队）打包一次。

与 `modules.chat.runtime.ChatRuntime` 同一套路：`main` 在 lifespan 里 build 一份放进
`app.state.knowledge_runtime`，路由通过 `request.app.state.knowledge_runtime` 取用；
集成测试可以先把 `app.state.knowledge_runtime` 换成用 fake 依赖构建的实例（与 chat 一致）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.ingest.parsers.url import FetchedUrl
from aics.modules.knowledge.service import KnowledgeService
from aics.providers.base import EmbeddingProvider, RerankProvider
from aics.storage import Storage
from aics.vectorstore import QdrantVectorStore

Enqueue = Callable[[str, Sequence[Any]], None]
Fetcher = Callable[[str], Awaitable[FetchedUrl]]


@dataclass(frozen=True)
class KnowledgeRuntime:
    sessions: async_sessionmaker[AsyncSession]
    store: Storage | None = None
    embedder: EmbeddingProvider | None = None
    vector: QdrantVectorStore | None = None
    reranker: RerankProvider | None = None
    enqueue: Enqueue | None = None
    fetcher: Fetcher | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """一个请求一个会话；`KnowledgeService` 自己提交，异常由这里回滚。"""
        async with self.sessions() as session:
            yield session

    def service(self, session: AsyncSession) -> KnowledgeService:
        return KnowledgeService(
            session,
            store=self.store,
            embedder=self.embedder,
            vector=self.vector,
            reranker=self.reranker,
            enqueue=self.enqueue,
            fetcher=self.fetcher,
        )
