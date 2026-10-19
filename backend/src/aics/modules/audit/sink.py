"""把 `core.audit.AuditEntry` 落进 `audit_logs`。

只在调用方的事务里 `session.add(...)`，**不 commit**：业务写成功而审计失败时整笔回滚，
反过来也一样（M4.1 的集成用例验证这一点）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from aics.core import audit as port
from aics.db.models.audit import AuditLog


class PgAuditSink:
    """`core.audit.AuditSink` 的 PostgreSQL 实现。"""

    async def write(self, session: AsyncSession, entry: port.AuditEntry) -> None:
        session.add(
            AuditLog(
                actor_type=entry.actor_type,
                actor_id=entry.actor_id,
                app_id=entry.app_id,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                diff=port.redact(entry.diff) if entry.diff else None,
                ip=entry.ip,
                user_agent=entry.user_agent,
                request_id=entry.request_id,
            )
        )
