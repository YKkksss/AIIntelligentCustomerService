"""`modules.apikeys` 管理接口的运行时（M4.5 ②）。

会话工厂 + 平台设置 + 审计端口 + Redis。`apikeys` 是独立模块：审计端口不能 import
`modules.audit`，由 `main.build_apikeys_admin_runtime` 注入；Redis 用于吊销 / 改限额后立刻
失效鉴权缓存（`{prefix}apikey:<prefix>`），让"收紧"在下一个请求就生效。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.audit import AuditSink
from aics.core.config import Settings
from aics.modules.apikeys.admin_service import ApiKeyAdminService


@dataclass(frozen=True)
class ApiKeyAdminRuntime:
    sessions: async_sessionmaker[AsyncSession]
    settings: Settings
    audit: AuditSink | None = None
    redis: Any = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as s:
            yield s

    def service(self, session: AsyncSession) -> ApiKeyAdminService:
        return ApiKeyAdminService(
            session,
            pepper=self.settings.api_key_pepper.get_secret_value(),
            redis=self.redis,
            redis_prefix=self.settings.redis_prefix,
            audit=self.audit,
        )
