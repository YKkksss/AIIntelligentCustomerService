"""转人工的持久化访问（M3.4 ②，会话生命周期与转人工 §3）。

状态迁移一律**条件单行 UPDATE**（`WHERE handoff_status = :expected AND status = 'active'`）：
受影响 0 行表示状态已被别人改掉（另一个坐席先抢到、队列超时先释放、会话被空闲扫描关闭），
service 据此重读并抛 409 `HANDOFF_STATE_CONFLICT`。并发抢单因此只有一个能成功，不需要额外锁。

读取走基类 `get()`（带 app_id 条件，跨应用天然 404）。列表用 `id` 升序做游标：`id` 是 UUIDv7，
单调递增，坐席平台补偿拉取时既不会漏也不会重。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from aics.db.models.conversation import Conversation
from aics.db.scoped import AppScopedRepository

#: 坐席需要关注的状态（§3.1；`none`/`released` 由 AI 接管）。
OPEN_STATUSES: tuple[str, ...] = ("pending", "accepted")


class HandoffRepository(AppScopedRepository[Conversation]):
    model = Conversation

    async def accept(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        agent_id: str,
        agent_name: str | None,
        at: datetime,
    ) -> int:
        """`pending → accepted`（抢单）；另一个坐席先抢到就返回 0。"""
        stmt = (
            sa.update(Conversation)
            .where(*self._where_id(app_id, conversation_id))
            .where(Conversation.status == "active", Conversation.handoff_status == "pending")
            .values(
                handoff_status="accepted",
                handoff_agent_id=agent_id,
                handoff_agent_name=agent_name,
                handoff_accepted_at=at,
            )
        )
        return int((await self.session.execute(stmt)).rowcount)

    async def release(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        expected: Sequence[str],
        end_reason: str,
        at: datetime,
    ) -> int:
        """`pending|accepted → released`；同时把 `last_activity_at` 置 now（§3.4）。

        `end_reason` 取 `HANDOFF_END_REASONS`（`agent` / `admin` / `queue_timeout` / `offline` /
        `accepted_idle`）。坐席交还时附带的 `note` 目前只进日志：`conversations` 没有对应列，
        结构化审计留给 M4.1 的 audit 模块（待解决问题清单 ISSUE-53）。
        """
        stmt = (
            sa.update(Conversation)
            .where(*self._where_id(app_id, conversation_id))
            .where(
                Conversation.status == "active",
                Conversation.handoff_status.in_(tuple(expected)),
            )
            .values(
                handoff_status="released",
                handoff_end_reason=end_reason,
                handoff_released_at=at,
                last_activity_at=at,
            )
        )
        return int((await self.session.execute(stmt)).rowcount)

    async def status_of(self, app_id: UUID, conversation_id: UUID) -> str | None:
        """当前 `handoff_status`（列级 SELECT，绕开 ORM 身份映射里的旧对象）。"""
        stmt = sa.select(Conversation.handoff_status).where(
            *self._where_id(app_id, conversation_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_open(
        self,
        app_id: UUID,
        *,
        status: str,
        limit: int,
        after: UUID | None = None,
    ) -> tuple[list[Conversation], bool]:
        """坐席平台的补偿拉取：`status` 命中且会话仍活跃，`id` 升序，多取一行判有无下一页。"""
        stmt = (
            self._scoped(app_id)
            .where(Conversation.status == "active", Conversation.handoff_status == status)
            .order_by(Conversation.id)
            .limit(limit + 1)
        )
        if after is not None:
            stmt = stmt.where(Conversation.id > after)
        rows = list((await self.session.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit

    # ------------------------------------------------- 超时扫描（§3.6 / §3.8 / §3.10）
    async def handoff_app_ids_all_apps(self) -> list[UUID]:
        """有进行中转人工会话的应用（`idle_sweep` 的入口；走 `ix_conversations_app_handoff`）。"""
        stmt = (
            sa.select(Conversation.app_id)
            .where(
                Conversation.status == "active",
                Conversation.handoff_status.in_(OPEN_STATUSES),
            )
            .distinct()
        )
        return list((await self.session.execute(stmt)).scalars())

    async def pending_due(
        self, app_id: UUID, *, before: datetime, limit: int
    ) -> list[tuple[UUID, datetime]]:
        """排队超时的候选：`pending` 且 `handoff_requested_at ≤ before`。

        返回 `(id, handoff_requested_at)`——条件更新要拿这个值做比对，防止「读完之后被接入」
        （§3.6 第 1 步）。"""
        stmt = (
            sa.select(Conversation.id, Conversation.handoff_requested_at)
            .where(
                Conversation.app_id == app_id,
                Conversation.status == "active",
                Conversation.handoff_status == "pending",
                Conversation.handoff_requested_at.is_not(None),
                Conversation.handoff_requested_at <= before,
            )
            .order_by(Conversation.handoff_requested_at)
            .limit(limit)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def accepted_due(
        self, app_id: UUID, *, before: datetime, limit: int
    ) -> list[tuple[UUID, datetime]]:
        """接入后无消息超时的候选：`accepted` 且 `last_activity_at ≤ before`（§3.8）。

        坐席消息经 `chat.service.append_agent_message` 写入，与 user 消息一样会推高
        `last_activity_at`，所以这里看的是「最后一次有人说话」。"""
        stmt = (
            sa.select(Conversation.id, Conversation.last_activity_at)
            .where(
                Conversation.app_id == app_id,
                Conversation.status == "active",
                Conversation.handoff_status == "accepted",
                Conversation.last_activity_at <= before,
            )
            .order_by(Conversation.last_activity_at)
            .limit(limit)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def queue_timeout(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        seen_requested_at: datetime,
        at: datetime,
    ) -> int:
        """`pending → released`（`queue_timeout`）+ 待回访 + 恢复空闲计时（§3.6）。"""
        stmt = (
            sa.update(Conversation)
            .where(*self._where_id(app_id, conversation_id))
            .where(
                Conversation.status == "active",
                Conversation.handoff_status == "pending",
                Conversation.handoff_requested_at == seen_requested_at,
            )
            .values(
                handoff_status="released",
                handoff_end_reason="queue_timeout",
                handoff_released_at=at,
                last_activity_at=at,
                needs_callback=True,
                callback_reason="queue_timeout",
                callback_requested_at=at,
            )
        )
        return int((await self.session.execute(stmt)).rowcount)

    async def close_accepted_idle(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        seen_activity: datetime,
        at: datetime,
    ) -> int:
        """`accepted → released`（`accepted_idle`）并直接关会话（§3.8）。"""
        stmt = (
            sa.update(Conversation)
            .where(*self._where_id(app_id, conversation_id))
            .where(
                Conversation.status == "active",
                Conversation.handoff_status == "accepted",
                Conversation.last_activity_at == seen_activity,
            )
            .values(
                handoff_status="released",
                handoff_end_reason="accepted_idle",
                handoff_released_at=at,
                status="closed",
                close_reason="handoff_idle",
                closed_at=at,
            )
        )
        return int((await self.session.execute(stmt)).rowcount)
