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
