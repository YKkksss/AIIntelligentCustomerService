"""M2.1 (integration): conversation_facts / conversation_fact_history constraints and append-only."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from aics.db.models import Conversation, ConversationFact, ConversationFactHistory

pytestmark = pytest.mark.integration


def _fact(conv: Conversation, **over: Any) -> ConversationFact:
    base: dict[str, Any] = {
        "app_id": conv.app_id,
        "conversation_id": conv.id,
        "key": "order_no",
        "value": "A123",
        "confidence": 0.9,
        "category": "order",
        "importance": 3,
        "source": "slot",
    }
    return ConversationFact(**(base | over))


@pytest.mark.parametrize(
    "over",
    [
        {"key": "password"},
        {"key": "other:Bad-Name"},
        {"value": ""},
        {"importance": 4},
        {"confidence": 1.2},
        {"source": "llm"},
        {"category": "misc"},
    ],
)
async def test_fact_constraints(pg: dict[str, Any], over: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        c = Conversation(app_id=pg["app_a"], channel="api")
        s.add(c)
        await s.flush()
        s.add(_fact(c, **over))
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_unique_key_per_conversation(pg: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        c = Conversation(app_id=pg["app_a"], channel="api")
        s.add(c)
        await s.flush()
        s.add(_fact(c))
        await s.flush()
        s.add(_fact(c, value="B456"))
        with pytest.raises(IntegrityError):
            await s.flush()


@pytest.mark.parametrize(
    ("op", "old", "new"),
    [("add", "x", "y"), ("update", None, "y"), ("retract", "x", "y"), ("add", None, None)],
)
async def test_history_op_shape(pg: dict[str, Any], op: str, old: Any, new: Any) -> None:
    async with pg["sessions"]() as s:
        c = Conversation(app_id=pg["app_a"], channel="api")
        s.add(c)
        await s.flush()
        f = _fact(c)
        s.add(f)
        await s.flush()
        s.add(
            ConversationFactHistory(
                fact_id=f.id,
                app_id=c.app_id,
                conversation_id=c.id,
                key=f.key,
                op=op,
                old_value=old,
                new_value=new,
                source="slot",
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_history_rejects_update_but_cascades_on_delete(pg: dict[str, Any]) -> None:
    sessions = pg["sessions"]
    async with sessions() as s:
        c = Conversation(app_id=pg["app_a"], channel="api")
        s.add(c)
        await s.flush()
        f = _fact(c)
        s.add(f)
        await s.flush()
        s.add(
            ConversationFactHistory(
                fact_id=f.id,
                app_id=c.app_id,
                conversation_id=c.id,
                key=f.key,
                op="add",
                new_value="A123",
                source="slot",
                confidence=0.9,
            )
        )
        await s.commit()
        cid = c.id
    async with sessions() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(
                text(
                    "UPDATE conversation_fact_history SET new_value = 'X' WHERE conversation_id = :c"
                ),
                {"c": cid},
            )
        await s.rollback()
    async with sessions() as s:
        await s.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": cid})
        await s.commit()
        n = await s.scalar(
            select(func.count())
            .select_from(ConversationFactHistory)
            .where(ConversationFactHistory.conversation_id == cid)
        )
        assert n == 0
