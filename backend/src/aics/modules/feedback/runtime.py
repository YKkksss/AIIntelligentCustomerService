"""feedback 运行时（M3.6 ②）。

反馈只写一张表、不碰模型与向量库，所以运行时只有会话工厂——与 `HandoffRuntime` 同一套路。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.modules.feedback.service import FeedbackService


@dataclass(frozen=True)
class FeedbackRuntime:
    sessions: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """一个请求一个会话；`FeedbackService` 自己提交，异常由这里回滚。"""
        async with self.sessions() as session:
            yield session

    def service(self, session: AsyncSession) -> FeedbackService:
        return FeedbackService(session)
