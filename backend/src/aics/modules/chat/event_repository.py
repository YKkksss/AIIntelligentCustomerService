"""`conversation_events` 的读取入口（M3.5 ①/②）。

写入只有一处：`chat.events.emit`（同事务、由 `conversations.event_seq` 分配 seq）。
这里只做读：事件流按 `seq > Last-Event-ID` 补发、后台排查按时间倒序翻页。
"""

from __future__ import annotations

from uuid import UUID

from aics.db.models.event import ConversationEvent
from aics.db.scoped import MAX_PAGE_SIZE, AppScopedRepository


class ConversationEventRepository(AppScopedRepository[ConversationEvent]):
    model = ConversationEvent

    async def list_after(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[ConversationEvent]:
        """`seq > after_seq` 的事件，旧→新（事件流的补发顺序）。"""
        stmt = (
            self._scoped(app_id)
            .where(
                ConversationEvent.conversation_id == conversation_id,
                ConversationEvent.seq > max(after_seq, 0),
            )
            .order_by(ConversationEvent.seq.asc())
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
        )
        return list((await self.session.execute(stmt)).scalars())

    async def last_seq(self, app_id: UUID, conversation_id: UUID) -> int:
        """该会话已有事件的最大 seq（没有事件时为 0）。"""
        stmt = (
            self._scoped(app_id)
            .with_only_columns(ConversationEvent.seq)
            .where(ConversationEvent.conversation_id == conversation_id)
            .order_by(ConversationEvent.seq.desc())
            .limit(1)
        )
        return int((await self.session.execute(stmt)).scalar_one_or_none() or 0)
