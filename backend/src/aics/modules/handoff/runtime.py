"""handoff 运行时：坐席接口只要一个会话工厂（M3.4 ②）。

与 `modules.chat.runtime.ChatRuntime` / `modules.knowledge.runtime.KnowledgeRuntime` 同一套路：
`main` 在 lifespan 里 build 一份放进 `app.state.handoff_runtime`，路由通过
`request.app.state.handoff_runtime` 取用；测试可以先换成用 fake 依赖构建的实例。

状态迁移只碰 `conversations` 与 `messages` 两张表：模型、向量库、记忆都不参与，所以这里没有
别的字段（不像 chat 要一整条 RAG 链路）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.modules.handoff.service import HandoffService


@dataclass(frozen=True)
class HandoffRuntime:
    sessions: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """一个请求一个会话；`HandoffService` 自己提交，异常由这里回滚。"""
        async with self.sessions() as session:
            yield session

    def service(self, session: AsyncSession) -> HandoffService:
        return HandoffService(session)
