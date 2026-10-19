"""`audit_logs` 的只读查询。

审计表不是租户表（`app_id` 可空、平台动作没有应用），所以是普通 repository 而不是
`AppScopedRepository`；游标用 `id` 降序（`id` 是 IDENTITY，天然按写入顺序且不重复）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from aics.db.models.audit import AuditLog

MAX_PAGE_SIZE = 200


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _query(
        self,
        *,
        app_id: UUID | None,
        action: str | None,
        actor_id: UUID | None,
        before_id: int | None,
    ) -> Select[tuple[AuditLog]]:
        stmt = select(AuditLog)
        if app_id is not None:
            stmt = stmt.where(AuditLog.app_id == app_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if before_id is not None:
            stmt = stmt.where(AuditLog.id < before_id)
        return stmt

    async def page(
        self,
        *,
        app_id: UUID | None = None,
        action: str | None = None,
        actor_id: UUID | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        stmt = (
            self._query(app_id=app_id, action=action, actor_id=actor_id, before_id=before_id)
            .order_by(AuditLog.id.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def actions(self) -> list[str]:
        stmt = select(AuditLog.action).distinct().order_by(AuditLog.action)
        result = await self.session.execute(stmt)
        return [str(row) for row in result.scalars().all()]

    def body(self, row: AuditLog) -> dict[str, Any]:
        return {
            "id": row.id,
            "action": row.action,
            "actor_type": row.actor_type,
            "actor_id": str(row.actor_id) if row.actor_id else None,
            "app_id": str(row.app_id) if row.app_id else None,
            "target_type": row.target_type,
            "target_id": str(row.target_id) if row.target_id else None,
            "diff": row.diff,
            "ip": row.ip,
            "user_agent": row.user_agent,
            "request_id": row.request_id,
            "created_at": row.created_at,
        }
