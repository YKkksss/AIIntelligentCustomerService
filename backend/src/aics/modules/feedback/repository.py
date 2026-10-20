"""反馈表的 repository（M3.6 ②）。

覆盖写用 PostgreSQL 的 `INSERT … ON CONFLICT (message_id) DO UPDATE`：一条消息只可能有一条
反馈（唯一键），重复提交改 `rating` / `comment` / `updated_at`，`id` 与 `created_at` 不变。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from aics.db.base import new_id
from aics.db.models.conversation import Message
from aics.db.models.feedback import Feedback
from aics.db.scoped import AppScopedRepository


class FeedbackRepository(AppScopedRepository[Feedback]):
    model = Feedback

    async def upsert(
        self,
        app_id: UUID,
        message_id: UUID,
        *,
        rating: int,
        comment: str | None = None,
        source: str = "api",
    ) -> Feedback:
        """按 `message_id` 覆盖写，返回落库后的行（`id` 保持首次提交时的值）。"""
        stmt = (
            pg_insert(Feedback)
            .values(
                id=new_id(),
                app_id=app_id,
                message_id=message_id,
                rating=rating,
                comment=comment,
                source=source,
                handled=False,
            )
            .on_conflict_do_update(
                index_elements=[Feedback.message_id],
                set_={
                    "rating": rating,
                    "comment": comment,
                    "source": source,
                    "updated_at": func.now(),
                },
            )
        )
        await self.session.execute(stmt)
        query = self._scoped(app_id).where(Feedback.message_id == message_id)
        return (await self.session.execute(query)).scalar_one()

    async def by_message(self, app_id: UUID, message_id: UUID) -> Feedback | None:
        query = self._scoped(app_id).where(Feedback.message_id == message_id)
        return (await self.session.execute(query)).scalar_one_or_none()

    # ------------------------------------------------------------ 运营面（M4.5 ③）
    async def page(
        self,
        app_id: UUID,
        *,
        rating: int | None = None,
        handled: bool | None = None,
        before: tuple[Any, UUID] | None = None,
        limit: int = 20,
    ) -> tuple[list[tuple[Feedback, Message]], bool]:
        """反馈按 `(created_at, id)` 倒序一页；返回 `(行, 是否还有更早的)`。"""
        stmt = (
            select(Feedback, Message)
            .join(Message, Message.id == Feedback.message_id)
            .where(Feedback.app_id == app_id)
            .order_by(Feedback.created_at.desc(), Feedback.id.desc())
        )
        if rating is not None:
            stmt = stmt.where(Feedback.rating == rating)
        if handled is not None:
            stmt = stmt.where(Feedback.handled.is_(handled))
        if before is not None:
            at, fid = before
            stmt = stmt.where(
                or_(Feedback.created_at < at, and_(Feedback.created_at == at, Feedback.id < fid))
            )
        rows = list((await self.session.execute(stmt.limit(limit + 1))).all())
        return rows[:limit], len(rows) > limit

    async def handled(self, app_id: UUID, feedback_id: UUID, *, handled: bool) -> Feedback | None:
        stmt = (
            update(Feedback)
            .where(Feedback.app_id == app_id, Feedback.id == feedback_id)
            .values(handled=handled, updated_at=func.now())
            .returning(Feedback.id)
            .execution_options(synchronize_session="evaluate")
        )
        if (await self.session.execute(stmt)).scalar_one_or_none() is None:
            return None
        query = self._scoped(app_id).where(Feedback.id == feedback_id)
        return (await self.session.execute(query)).scalar_one_or_none()
