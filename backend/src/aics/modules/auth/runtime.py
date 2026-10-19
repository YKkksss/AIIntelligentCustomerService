"""`modules.auth` 的运行时装配（M4.1 ②）：与其它模块同构——一个 `AuthRuntime` 持有 sessionmaker，
`service(session)` 造业务对象，`authorizer()` 造 `core.security.Authorizer` 的实例。

`audit` 为空时登录不写审计（M4.1 ③ 才把 `PgAuditSink` 接进来），但接口已经按端口写好。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.audit import AuditSink
from aics.core.config import Settings
from aics.modules.auth.authorizer import AuthAuthorizer
from aics.modules.auth.service import AuthService


@dataclass(frozen=True)
class AuthRuntime:
    sessions: async_sessionmaker
    settings: Settings
    redis: Any | None = None
    audit: AuditSink | None = None

    def service(self, session: AsyncSession) -> AuthService:
        return AuthService(session, settings=self.settings, redis=self.redis, audit=self.audit)

    def authorizer(self) -> AuthAuthorizer:
        return AuthAuthorizer(self.sessions, settings=self.settings, redis=self.redis)


async def session_scope(runtime: AuthRuntime) -> AsyncIterator[AsyncSession]:
    async with runtime.sessions() as session:
        yield session
