"""`modules.apps` 管理接口的运行时（M4.2）。

会话工厂 + 平台设置 + 审计端口 + 任务投递器。`apps` 是独立模块：它不能 import `aics.tasks`
（分层合同）也不能 import `modules.audit`（独立性合同），所以审计端与投递器都由
`main.build_apps_admin_runtime` 注入。删除应用（M4.2 ③）还会用到 `storage` 与 `vector`，
这里一并留好字段。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.audit import AuditSink
from aics.core.config import Settings
from aics.modules.apps.admin_service import AppsAdminService


@dataclass(frozen=True)
class AppsAdminRuntime:
    sessions: async_sessionmaker[AsyncSession]
    settings: Settings
    audit: AuditSink | None = None
    enqueue: Any = None
    storage: Any = None
    vector: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as s:
            yield s

    async def service(self, session: AsyncSession) -> AppsAdminService:
        return await AppsAdminService.load(
            session, settings=self.settings, audit=self.audit, enqueue=self.enqueue
        )
