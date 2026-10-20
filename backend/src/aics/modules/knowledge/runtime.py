"""knowledge 运行时：把开放接口需要的依赖（会话、存储、嵌入器、Qdrant、入队）打包一次。

与 `modules.chat.runtime.ChatRuntime` 同一套路：`main` 在 lifespan 里 build 一份放进
`app.state.knowledge_runtime`，路由通过 `request.app.state.knowledge_runtime` 取用；
集成测试可以先把 `app.state.knowledge_runtime` 换成用 fake 依赖构建的实例（与 chat 一致）。

字段名对齐 `KnowledgeService` 的形参：`storage` = 文件存储（`aics.storage.Storage`），
`vector` = Qdrant（`QdrantVectorStore`，service 里的 `store`）。写错过一次（把 storage 当
store 传），`tests/unit/test_knowledge_api.py::test_runtime_wires_service_dependencies` 兜底。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.audit import AuditSink
from aics.ingest.parsers.url import FetchedUrl
from aics.modules.knowledge.service import KnowledgeService
from aics.providers.base import EmbeddingProvider, RerankProvider
from aics.storage import Storage, get_storage
from aics.vectorstore import QdrantVectorStore

Enqueue = Callable[[str, Sequence[Any]], None]
Fetcher = Callable[[str], Awaitable[FetchedUrl]]


@dataclass(frozen=True)
class KnowledgeRuntime:
    sessions: async_sessionmaker[AsyncSession]
    storage: Storage | None = None
    vector: QdrantVectorStore | None = None
    embedder: EmbeddingProvider | None = None
    enqueue: Enqueue | None = None
    fetcher: Fetcher | None = None
    #: 管理版接口（M4.3 ②）用：命中测试的重排器与写操作审计端口。
    reranker: RerankProvider | None = None
    audit: AuditSink | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """一个请求一个会话；`KnowledgeService` 自己提交，异常由这里回滚。"""
        async with self.sessions() as session:
            yield session

    def service(self, session: AsyncSession) -> KnowledgeService:
        return KnowledgeService(
            session,
            storage=self.storage or get_storage(),
            store=self.vector,
            embedder=self.embedder,
            enqueue=self.enqueue,
            fetcher=self.fetcher,
            audit=self.audit,
        )
