"""M3.6 ② 集成：反馈的覆盖写与拒绝口径（真 PG）。

只跑 SQL 语义：唯一键 + `ON CONFLICT DO UPDATE`（`id` / `created_at` 不变、`updated_at` 变）、
两条拒绝（消息不存在、不是 assistant 消息）、跨应用看不到别人的消息。路由层的参数校验由
`tests/unit/test_feedback_api.py` 覆盖。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from aics.core.errors import NotFoundError, ValidationAppError
from aics.db.models import Feedback
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage
from aics.modules.feedback.repository import FeedbackRepository
from aics.modules.feedback.service import FeedbackService

pytestmark = pytest.mark.integration


async def new_message(pg: dict[str, Any], app_id: UUID, *, role: str = "assistant") -> UUID:
    """建一个会话 + 一条消息，返回消息 id。"""
    async with pg["sessions"]() as s:
        conv = await ConversationRepository(s).create(
            app_id, NewConversation(channel="api", external_user_id="u1")
        )
        data = NewMessage(
            role=role,  # type: ignore[arg-type]
            content="退货请在订单页申请退款",
            answer_type="answer" if role == "assistant" else None,
        )
        msg = await MessageRepository(s).append(app_id, conv.id, data)
        await s.commit()
        return msg.id


async def rows(pg: dict[str, Any], app_id: UUID) -> list[Feedback]:
    async with pg["sessions"]() as s:
        found = await s.execute(select(Feedback).where(Feedback.app_id == app_id))
        return list(found.scalars())


async def submit(
    pg: dict[str, Any], app_id: UUID, message_id: UUID, *, rating: int, comment: str | None = None
) -> Feedback:
    async with pg["sessions"]() as s:
        return await FeedbackService(s).submit(app_id, message_id, rating=rating, comment=comment)


async def test_submit_writes_one_row(pg: dict[str, Any]) -> None:
    app_id = pg["app_a"]
    message_id = await new_message(pg, app_id)

    row = await submit(pg, app_id, message_id, rating=-1, comment="答非所问")

    assert (row.app_id, row.message_id, row.rating, row.comment) == (
        app_id,
        message_id,
        -1,
        "答非所问",
    )
    assert (row.source, row.handled, row.eval_case_id) == ("api", False, None)
    assert [r.id for r in await rows(pg, app_id)] == [row.id]


async def test_second_submit_overwrites_and_keeps_the_id(pg: dict[str, Any]) -> None:
    app_id = pg["app_a"]
    message_id = await new_message(pg, app_id)
    first = await submit(pg, app_id, message_id, rating=-1, comment="第一次")
    await asyncio.sleep(0.01)  # `updated_at` 由 now() 给，同毫秒内覆盖看不出差异

    second = await submit(pg, app_id, message_id, rating=1)

    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at
    assert (second.rating, second.comment) == (1, None)
    assert len(await rows(pg, app_id)) == 1  # 唯一键保证只有一行


async def test_feedback_of_another_app_cannot_touch_the_row(pg: dict[str, Any]) -> None:
    app_a, app_b = pg["app_a"], pg["app_b"]
    message_id = await new_message(pg, app_a)
    mine = await submit(pg, app_a, message_id, rating=1)

    with pytest.raises(NotFoundError):
        await submit(pg, app_b, message_id, rating=-1)

    remaining = await rows(pg, app_a)
    assert [(r.id, r.rating) for r in remaining] == [(mine.id, 1)]


async def test_missing_message_is_404(pg: dict[str, Any]) -> None:
    message_id = uuid4()

    with pytest.raises(NotFoundError) as err:
        await submit(pg, pg["app_a"], message_id, rating=1)

    assert err.value.details == {"message_id": str(message_id)}


async def test_non_assistant_message_is_rejected(pg: dict[str, Any]) -> None:
    app_id = pg["app_a"]
    message_id = await new_message(pg, app_id, role="user")

    with pytest.raises(ValidationAppError) as err:
        await submit(pg, app_id, message_id, rating=1)

    assert err.value.details["errors"][0]["loc"] == ["path", "message_id"]
    assert await rows(pg, app_id) == []


async def test_repository_upsert_and_lookup(pg: dict[str, Any]) -> None:
    app_id = pg["app_a"]
    message_id = await new_message(pg, app_id)

    async with pg["sessions"]() as s:
        repo = FeedbackRepository(s)
        await repo.upsert(app_id, message_id, rating=1)
        await s.commit()
        found = await repo.by_message(app_id, message_id)
        assert found is not None
        assert (found.rating, found.comment, found.source) == (1, None, "api")
        assert await repo.by_message(app_id, uuid4()) is None
