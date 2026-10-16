"""M2.1 (integration): constraints, cascades and seed data of the conversation tables."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aics.db.models import Conversation, Message, SystemSetting

pytestmark = pytest.mark.integration


async def test_seeded_default_session_config(pg: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        row = await s.get(SystemSetting, "default_session_config")
    assert row is not None
    assert row.value["idle_close_minutes"] == 10


async def _conv(s: Any, app_id: Any) -> Conversation:
    c = Conversation(app_id=app_id, channel="api")
    s.add(c)
    await s.flush()
    return c


@pytest.mark.parametrize(
    "fields",
    [
        {"role": "robot", "content": "x"},
        {"role": "user", "content": "x", "kind": "idle_notice"},
        {"role": "user", "content": "x", "answer_type": "answer"},
        {"role": "user", "content": "x", "handoff": True},
        {"role": "assistant", "content": "x", "intent_confidence": 1.5},
        {"role": "assistant", "content": "x", "token_count": -1},
    ],
)
async def test_message_check_constraints(pg: dict[str, Any], fields: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        c = await _conv(s, pg["app_a"])
        s.add(Message(app_id=pg["app_a"], conversation_id=c.id, **fields))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_closed_requires_closed_at(pg: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        s.add(Conversation(app_id=pg["app_a"], channel="api", status="closed"))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_tsv_generated_and_cascade_delete(pg: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        c = await _conv(s, pg["app_a"])
        s.add(
            Message(
                app_id=pg["app_a"],
                conversation_id=c.id,
                role="user",
                content="退货",
                search_text="退货 运费",
            )
        )
        await s.flush()
        hit = await s.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id == c.id,
                Message.tsv.op("@@")(func.to_tsquery("simple", "'运费'")),
            )
        )
        assert hit == 1
        await s.delete(c)
        await s.flush()
        left = await s.scalar(
            select(func.count()).select_from(Message).where(Message.conversation_id == c.id)
        )
        assert left == 0
        await s.rollback()
