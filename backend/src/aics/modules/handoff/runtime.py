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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.modules.chat import events
from aics.modules.handoff.service import HandoffService


@dataclass(frozen=True)
class HandoffRuntime:
    sessions: async_sessionmaker[AsyncSession]
    # 坐席动作也要推进会话事件流（M3.5 ②）；没有 Redis 时事件只落库不广播。
    redis: Any = None
    redis_prefix: str = "aics:"

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """一个请求一个会话；`HandoffService` 自己提交，异常由这里回滚。"""
        async with self.sessions() as session:
            yield session

    def publisher(self) -> events.Publisher | None:
        if self.redis is None:
            return None
        return events.RedisPublisher(self.redis, prefix=self.redis_prefix)

    def service(self, session: AsyncSession) -> HandoffService:
        return HandoffService(session, publisher=self.publisher())
