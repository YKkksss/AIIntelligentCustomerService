"""审计模块的运行时：一个 sessionmaker + 查询服务 + 写入端（`PgAuditSink`）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.modules.audit.service import AuditService
from aics.modules.audit.sink import PgAuditSink


@dataclass(frozen=True)
class AuditRuntime:
    sessions: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    def service(self, session: AsyncSession) -> AuditService:
        return AuditService(session)

    def sink(self) -> PgAuditSink:
        return PgAuditSink()
