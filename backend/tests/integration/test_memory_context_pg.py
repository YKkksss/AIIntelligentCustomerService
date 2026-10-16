"""M2.5 ① (integration): load_context on PostgreSQL messages / summary / facts."""

from __future__ import annotations

from typing import Any

import pytest

from aics.core.errors import ConversationNotFoundError
from aics.memory.context import REFUSED_PLACEHOLDER, ContextQuery
from aics.memory.repository import PgConversationStore
from aics.memory.schemas import ConversationSummary, FactWrite
from aics.memory.service import MemoryService, pg_units
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage

pytestmark = pytest.mark.integration


async def seed(pg: dict[str, Any], n: int, *, app: str = "app_a") -> tuple[Any, list[Any]]:
    async with pg["sessions"]() as s:
        app_id = pg[app]
        conv = await ConversationRepository(s).create(app_id, NewConversation(channel="api"))
        msgs = MessageRepository(s)
        ids = []
        for i in range(n):
            u = await msgs.append(app_id, conv.id, NewMessage(role="user", content=f"第{i}轮问题"))
            kw = {"answer_type": "refused" if i == 1 else "answer"}
            a = await msgs.append(
                app_id, conv.id, NewMessage(role="assistant", content=f"第{i}轮回答" * 20, **kw)
            )
            ids.append((u.id, a.id))
        await msgs.append(
            app_id, conv.id, NewMessage(role="system", content="提醒", kind="idle_notice")
        )
        await s.commit()
        return conv.id, ids


async def test_load_context_pg_budget_summary_facts_and_isolation(pg: dict[str, Any]) -> None:
    cid, ids = await seed(pg, 12)
    svc = MemoryService(pg_units(pg["sessions"]))
    q = ContextQuery(
        app_id=pg["app_a"], conversation_id=cid, question="新问题", budget=3000, keep_recent_turns=2
    )
    ctx = await svc.load_context(q)
    assert ctx.recent_turns == 12 and ctx.flags == []
    assert ctx.history[3].content == REFUSED_PLACEHOLDER
    assert all(m.content != "提醒" for m in ctx.history)

    async with pg["sessions"]() as s:
        st = PgConversationStore(s)
        assert await st.save_summary(
            pg["app_a"], cid, ConversationSummary(user_goal="退货"), ids[5][1], 0
        )
        await st.add_fact(
            pg["app_a"],
            cid,
            FactWrite(key="order_no", value="A20261014001", source="slot", confidence=0.8),
        )
        await s.commit()
    ctx = await svc.load_context(
        ContextQuery(
            app_id=pg["app_a"],
            conversation_id=cid,
            question="新问题",
            budget=400,
            keep_recent_turns=2,
        )
    )
    assert ctx.summary["user_goal"] == "退货" and ctx.summary_version == 1
    assert ctx.facts == {"order_no": "A20261014001"}
    assert ctx.history[0].content.startswith("第") and "第5轮问题" not in [
        m.content for m in ctx.history
    ]
    assert 2 <= ctx.recent_turns < 6 and "history_gap" in ctx.flags

    with pytest.raises(ConversationNotFoundError):
        await svc.load_context(
            ContextQuery(
                app_id=pg["app_b"],
                conversation_id=cid,
                question="x",
                budget=100,
                keep_recent_turns=1,
            )
        )
