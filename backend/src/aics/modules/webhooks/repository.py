"""`webhook_deliveries` 的 repository（M3.5 ③）。

投递记录与事件一一对应（`id == event_id`），所以这里的方法都带 `app_id` 条件：
管理接口按应用翻页，投递任务按 id 取一条。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update

from aics.db.models.event import WebhookDelivery
from aics.db.scoped import MAX_PAGE_SIZE, AppScopedRepository


class WebhookDeliveryRepository(AppScopedRepository[WebhookDelivery]):
    model = WebhookDelivery

    async def list_recent(
        self,
        app_id: UUID,
        *,
        status: str | None = None,
        limit: int = 20,
        before: datetime | None = None,
    ) -> list[WebhookDelivery]:
        """最近的投递记录（新→旧），`before` 按 `created_at` 往前翻页。"""
        stmt = self._scoped(app_id)
        if status is not None:
            stmt = stmt.where(WebhookDelivery.status == status)
        if before is not None:
            stmt = stmt.where(WebhookDelivery.created_at < before)
        stmt = stmt.order_by(WebhookDelivery.created_at.desc()).limit(
            min(max(limit, 1), MAX_PAGE_SIZE)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def due_all_apps(self, *, before: datetime, limit: int = 100) -> list[UUID]:
        """到期该重试的投递 id（`pending` 且 `next_attempt_at <= before`）。

        跨应用的系统级扫描（`aics.webhook_retry_sweep`）：加 `FOR UPDATE SKIP LOCKED`，
        多个 worker 同时扫也不会重复入队。
        """
        stmt = (
            select(WebhookDelivery.id)
            .where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_attempt_at.is_not(None),
                WebhookDelivery.next_attempt_at <= before,
            )
            .order_by(WebhookDelivery.next_attempt_at.asc())
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            .with_for_update(skip_locked=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def mark_delivered(self, app_id: UUID, id: UUID, *, at: datetime) -> int:
        """`pending → delivered`（幂等：只有还没结束的投递会被改）。"""
        return await self._finish(
            app_id,
            id,
            status="delivered",
            values={"delivered_at": at, "next_attempt_at": None},
        )

    async def mark_attempt(
        self,
        app_id: UUID,
        id: UUID,
        *,
        status_code: int | None,
        error: str | None,
        next_attempt_at: datetime | None,
        failed: bool,
    ) -> int:
        """记一次尝试：`attempts += 1`，并按结果决定继续等重试还是标记 `failed`。"""
        values: dict[str, object] = {
            "attempts": WebhookDelivery.attempts + 1,
            "last_status_code": status_code,
            "last_error": (error or "")[:500] or None,
            "next_attempt_at": next_attempt_at,
        }
        if failed:
            values["status"] = "failed"
            values["delivered_at"] = None
        return int(
            (
                await self.session.execute(
                    self._update(app_id, id)
                    .where(WebhookDelivery.status == "pending")
                    .values(**values)
                )
            ).rowcount
        )

    async def reset_for_redelivery(self, app_id: UUID, id: UUID, *, at: datetime) -> int:
        """人工重发：`failed` / `delivered` → `pending`，`attempts` 归零、立即到期。"""
        stmt = (
            self._update(app_id, id)
            .where(WebhookDelivery.status.in_(("failed", "delivered")))
            .values(
                status="pending",
                attempts=0,
                last_error=None,
                last_status_code=None,
                delivered_at=None,
                next_attempt_at=at,
            )
        )
        return int((await self.session.execute(stmt)).rowcount)

    async def count_by_status(self, app_id: UUID) -> dict[str, int]:
        """按状态计数（管理接口的概览行）。"""
        stmt = (
            select(WebhookDelivery.status, func.count())
            .where(WebhookDelivery.app_id == app_id)
            .group_by(WebhookDelivery.status)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    # ---- 内部 ----
    def _update(self, app_id: UUID, id: UUID) -> Any:
        return update(WebhookDelivery).where(
            WebhookDelivery.app_id == app_id, WebhookDelivery.id == id
        )

    async def _finish(
        self, app_id: UUID, id: UUID, *, status: str, values: dict[str, object]
    ) -> int:
        stmt = (
            self._update(app_id, id)
            .where(WebhookDelivery.status == "pending")
            .values(status=status, **values)
        )
        return int((await self.session.execute(stmt)).rowcount)
