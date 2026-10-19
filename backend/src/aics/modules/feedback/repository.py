"""反馈表的 repository（M3.6 ②）。

覆盖写用 PostgreSQL 的 `INSERT … ON CONFLICT (message_id) DO UPDATE`：一条消息只可能有一条
反馈（唯一键），重复提交改 `rating` / `comment` / `updated_at`，`id` 与 `created_at` 不变。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from aics.db.base import new_id
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
