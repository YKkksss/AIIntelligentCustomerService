"""反馈的运营面（M4.5 ③）：列表、按会话上下文查看与「标记已处理」。

反馈本身在开放接口里只有一个「覆盖写」的动作（`POST …/messages/{id}/feedback`，M3.6）；
运营面要的是反过来：跨会话列出点踩/点赞，看到评论与对应回答，并把它勾掉。
`handled` 只影响运营视图，不改变用户已经提交的评价（覆盖写会把它重置为未处理）。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.audit import AuditEntry, AuditSink, diff
from aics.core.errors import NotFoundError, ValidationAppError
from aics.core.masking import mask_text
from aics.db.models.conversation import Message
from aics.db.models.feedback import Feedback
from aics.modules.feedback.repository import FeedbackRepository

HANDLE_ACTION = "feedback.handle"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def encode_cursor(row: Feedback) -> str:
    raw = json.dumps({"t": row.created_at.isoformat(), "id": str(row.id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(raw: str | None) -> tuple[Any, UUID] | None:
    if not raw:
        return None
    from datetime import datetime

    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return datetime.fromisoformat(str(payload["t"])), UUID(str(payload["id"]))
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ValidationAppError(
            message="游标无效",
            details={"errors": [{"loc": ["cursor"], "msg": "无法解析的分页游标"}]},
        ) from None


def feedback_row(feedback: Feedback, message: Message, *, mask: bool = False) -> dict[str, Any]:
    """一条反馈：带被评价回答的摘要与所属会话，运营不用再点开详情。"""
    content = message.content or ""
    return {
        "id": str(feedback.id),
        "message_id": str(feedback.message_id),
        "conversation_id": str(message.conversation_id),
        "rating": feedback.rating,
        "comment": None
        if feedback.comment is None
        else (mask_text(feedback.comment) if mask else feedback.comment),
        "source": feedback.source,
        "handled": feedback.handled,
        "created_at": _iso(feedback.created_at),
        "updated_at": _iso(feedback.updated_at),
        "message": {
            "role": message.role,
            "intent": message.intent,
            "excerpt": (mask_text(content) if mask else content)[:240],
        },
    }


@dataclass
class FeedbackAdminService:
    """运营面读写入口；一个实例对应一个数据库会话。"""

    session: AsyncSession
    audit: AuditSink | None = None

    @property
    def repo(self) -> FeedbackRepository:
        return FeedbackRepository(self.session)

    async def list(
        self,
        app_id: UUID,
        *,
        rating: int | None = None,
        handled: bool | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
        mask: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, MAX_LIMIT))
        rows, has_more = await self.repo.page(
            app_id,
            rating=rating,
            handled=handled,
            before=decode_cursor(cursor),
            limit=limit,
        )
        items = [feedback_row(f, m, mask=mask) for f, m in rows]
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(rows[-1][0])
        return {"items": items, "next_cursor": next_cursor}

    async def handle(
        self,
        app_id: UUID,
        feedback_id: UUID,
        *,
        handled: bool,
        actor_id: UUID | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self.repo.get(app_id, feedback_id)
        if row is None:
            raise NotFoundError(
                message="反馈不存在或不属于本应用", details={"feedback_id": str(feedback_id)}
            )
        before = row.handled
        if before != handled:
            updated = await self.repo.handled(app_id, feedback_id, handled=handled)
            if updated is None:
                raise NotFoundError(
                    message="反馈不存在或不属于本应用",
                    details={"feedback_id": str(feedback_id)},
                )
            row = updated
        message = (
            await self.session.execute(select(Message).where(Message.id == row.message_id))
        ).scalar_one_or_none()
        await self._audit(
            app_id,
            feedback_id,
            actor_id=actor_id,
            meta=meta,
            payload=diff({"handled": before}, {"handled": row.handled}),
        )
        await self.session.commit()
        if message is None:  # 消息被保留期清理时，仍返回反馈本身（不含上下文）
            return {
                "id": str(row.id),
                "message_id": str(row.message_id),
                "conversation_id": None,
                "rating": row.rating,
                "comment": row.comment,
                "source": row.source,
                "handled": row.handled,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
                "message": None,
            }
        return feedback_row(row, message)

    async def _audit(
        self,
        app_id: UUID,
        feedback_id: UUID,
        *,
        actor_id: UUID | None,
        meta: dict[str, Any] | None,
        payload: dict[str, Any] | None,
    ) -> None:
        if self.audit is None or actor_id is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=HANDLE_ACTION,
                actor_type="user",
                actor_id=actor_id,
                app_id=app_id,
                target_type="feedback",
                target_id=feedback_id,
                diff=payload,
                ip=(meta or {}).get("ip"),
                user_agent=(meta or {}).get("user_agent"),
                request_id=(meta or {}).get("request_id"),
            ),
        )
