"""反馈的领域动作（M3.6 ②，开放接口参考 §2）。

只有 `role="assistant"` 的消息可以反馈：点赞/点踩的对象是「回答」，用户自己的提问、坐席的人工
回复、系统提示都不该出现在反馈统计里。第二道闸是唯一键（一条消息一条反馈），所以本表不需要
额外的去重逻辑，重复提交就是覆盖。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.errors import NotFoundError, ValidationAppError
from aics.db.models.feedback import Feedback
from aics.modules.chat.repository import MessageRepository
from aics.modules.feedback.repository import FeedbackRepository


class FeedbackService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        messages: MessageRepository | None = None,
        feedback: FeedbackRepository | None = None,
    ) -> None:
        self.session = session
        self.messages = messages or MessageRepository(session)
        self.feedback = feedback or FeedbackRepository(session)

    async def submit(
        self,
        app_id: UUID,
        message_id: UUID,
        *,
        rating: int,
        comment: str | None = None,
    ) -> Feedback:
        """写入或覆盖一条反馈，返回落库的行。

        错误口径（ISSUE-67）：消息不存在与消息不属于本应用都回 404 `NOT_FOUND`（`details`
        带 `message_id`），不新增 `MESSAGE_NOT_FOUND` 码——错误码表是对外契约。
        """
        message = await self.messages.get(app_id, message_id)
        if message is None:
            raise NotFoundError(details={"message_id": str(message_id)})
        if message.role != "assistant":
            raise ValidationAppError(
                details={
                    "errors": [
                        {"loc": ["path", "message_id"], "msg": "只有 assistant 消息可以反馈"}
                    ]
                }
            )
        row = await self.feedback.upsert(
            app_id, message_id, rating=rating, comment=(comment or "").strip() or None
        )
        await self.session.commit()
        return row
