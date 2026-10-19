"""审计查询（`GET /api/v1/admin/audit`）：`id` 降序游标分页。"""

from __future__ import annotations

import base64
import binascii
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.errors import ValidationAppError
from aics.modules.audit.repository import AuditRepository

DEFAULT_PAGE_SIZE = 50
CURSOR_PREFIX = "a:"


def _encode(entry_id: int) -> str:
    raw = f"{CURSOR_PREFIX}{entry_id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValidationAppError(
            details={"errors": [{"loc": ["query", "cursor"], "msg": "无效的游标"}]}
        ) from exc
    if not raw.startswith(CURSOR_PREFIX):
        raise ValidationAppError(
            details={"errors": [{"loc": ["query", "cursor"], "msg": "无效的游标"}]}
        )
    try:
        value = int(raw[len(CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationAppError(
            details={"errors": [{"loc": ["query", "cursor"], "msg": "无效的游标"}]}
        ) from exc
    if value <= 0:
        raise ValidationAppError(
            details={"errors": [{"loc": ["query", "cursor"], "msg": "无效的游标"}]}
        )
    return value


class AuditService:
    def __init__(self, session: AsyncSession, *, repo: AuditRepository | None = None) -> None:
        self.session = session
        self.repo = repo or AuditRepository(session)

    async def page(
        self,
        *,
        app_id: UUID | None = None,
        action: str | None = None,
        actor_id: UUID | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        before_id = _decode(cursor) if cursor else None
        rows = await self.repo.page(
            app_id=app_id,
            action=action,
            actor_id=actor_id,
            before_id=before_id,
            limit=page_size + 1,
        )
        items = rows[:page_size]
        next_cursor = _encode(items[-1].id) if len(rows) > page_size and items else None
        return {
            "items": [self.repo.body(row) for row in items],
            "next_cursor": next_cursor,
        }

    async def actions(self) -> list[str]:
        return await self.repo.actions()
