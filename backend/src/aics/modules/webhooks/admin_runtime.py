"""`modules.webhooks` 管理接口的运行时（M4.5 ④）：会话工厂 + 平台设置 + 审计端口。

模块独立：审计端口由 `main.build_webhooks_admin_runtime` 注入，不 import `modules.audit`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.audit import AuditSink
from aics.core.config import Settings
from aics.modules.webhooks.admin_service import WebhookAdminService


@dataclass(frozen=True)
class WebhookAdminRuntime:
    sessions: async_sessionmaker[AsyncSession]
    settings: Settings
    audit: AuditSink | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as s:
            yield s

    def service(self, session: AsyncSession) -> WebhookAdminService:
        return WebhookAdminService(session, settings=self.settings, audit=self.audit)
