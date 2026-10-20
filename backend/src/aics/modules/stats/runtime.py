"""统计运行时（M4.6 ②）。

只读 `app_daily_stats`、写也只有聚合任务：运行时只有会话工厂，没有模型 / 向量库依赖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.modules.stats.service import StatsService


@dataclass(frozen=True)
class StatsRuntime:
    sessions: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """一个请求一个会话；`StatsService` 自己提交，异常由这里回滚。"""
        async with self.sessions() as session:
            yield session

    def service(self, session: AsyncSession) -> StatsService:
        return StatsService(session)
