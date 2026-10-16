"""M2.1 (integration): ConversationStore contract on PostgreSQL + append-only history."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from tests.memory_store_contract import CONTRACT, Harness, fw

from aics.db.models import Conversation, ConversationFactHistory
from aics.memory.repository import PgConversationStore
from aics.memory.schemas import ConversationSummary
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage

pytestmark = pytest.mark.integration


def _harness(s: Any, pg: dict[str, Any]) -> Harness:
    convs, msgs = ConversationRepository(s), MessageRepository(s)

    async def new_conversation(app_id: UUID) -> UUID:
        return (await convs.create(app_id, NewConversation(channel="api"))).id

    async def add_message(app_id: UUID, cid: UUID, role: str, content: str, **kw: Any) -> UUID:
        return (await msgs.append(app_id, cid, NewMessage(role=role, content=content, **kw))).id

    return Harness(PgConversationStore(s), pg["app_a"], pg["app_b"], new_conversation, add_message)


@pytest.mark.parametrize("check", CONTRACT, ids=lambda f: f.__name__)
async def test_pg_contract(pg: dict[str, Any], check: Any) -> None:
    async with pg["sessions"]() as s:
        await check(_harness(s, pg))
        await s.commit()


async def test_summary_history_metadata(pg: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        h = _harness(s, pg)
        c = await h.new_conversation(h.app_a)
        m = await h.add_message(h.app_a, c, "user", "hi")
        for v in range(7):
            ok = await h.store.save_summary(h.app_a, c, ConversationSummary(user_goal=str(v)), m, v)
            assert ok
        await s.commit()
        conv = await s.get(Conversation, c)
        assert conv is not None
        await s.refresh(conv)
        assert [e["version"] for e in conv.metadata_["summary_history"]] == [3, 4, 5, 6, 7]
        assert conv.summary == {
            "user_goal": "6",
            "confirmed_info": [],
            "solutions_given": [],
            "open_issues": [],
        }


async def test_history_written_in_same_transaction(pg: dict[str, Any]) -> None:
    """A rolled-back update leaves neither the new value nor a history row."""
    sessions = pg["sessions"]
    async with sessions() as s:
        h = _harness(s, pg)
        c = await h.new_conversation(h.app_a)
        await h.store.add_fact(h.app_a, c, fw())
        await s.commit()
    async with sessions() as s:
        h = _harness(s, pg)
        await h.store.update_fact(h.app_a, c, fw(value="B456"), expected_version=1)
        await s.rollback()
    async with sessions() as s:
        h = _harness(s, pg)
        fact = await h.store.get_fact(h.app_a, c, "order_no")
        assert fact is not None and fact.value == "A123" and fact.version == 1
        n = await s.scalar(
            select(func.count())
            .select_from(ConversationFactHistory)
            .where(ConversationFactHistory.conversation_id == c)
        )
        assert n == 1


async def test_stale_version_loses_race(pg: dict[str, Any]) -> None:
    """Two writers read version 1; the second compare-and-set loses (returns None)."""
    sessions = pg["sessions"]
    async with sessions() as s:
        h = _harness(s, pg)
        c = await h.new_conversation(h.app_a)
        await h.store.add_fact(h.app_a, c, fw())
        await s.commit()
    async with sessions() as s1:
        r1 = await PgConversationStore(s1).update_fact(
            pg["app_a"], c, fw(value="B1"), expected_version=1
        )
        await s1.commit()
    async with sessions() as s2:
        r2 = await PgConversationStore(s2).update_fact(
            pg["app_a"], c, fw(value="B2"), expected_version=1
        )
        await s2.commit()
    assert r1 is not None and r2 is None
