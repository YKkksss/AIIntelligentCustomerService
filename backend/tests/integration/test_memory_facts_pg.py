"""M2.5 ② (integration): fact reconciliation on PostgreSQL, concurrency and isolation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aics.core.errors import ConversationNotFoundError
from aics.memory.facts import FactCandidate
from aics.memory.repository import PgConversationStore
from aics.memory.service import MemoryService, pg_units
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage

pytestmark = pytest.mark.integration


async def conv_with(pg: dict[str, Any], n: int) -> tuple[Any, list[Any]]:
    async with pg["sessions"]() as s:
        conv = await ConversationRepository(s).create(pg["app_a"], NewConversation(channel="api"))
        ids = [
            (
                await MessageRepository(s).append(
                    pg["app_a"], conv.id, NewMessage(role="user", content=f"消息{i}")
                )
            ).id
            for i in range(n)
        ]
        await s.commit()
        return conv.id, ids


def c(key: str, value: str | None, mid: Any, src: str = "slot", retract: bool = False) -> Any:
    return FactCandidate(key, value, 0.9, src, mid, retract)  # type: ignore[arg-type]


async def test_correction_history_and_stale_compaction_pg(pg: dict[str, Any]) -> None:
    cid, m = await conv_with(pg, 4)
    svc = MemoryService(pg_units(pg["sessions"]))
    app = pg["app_a"]
    out = await svc.capture_slots(
        app,
        cid,
        {"order_no": "A20261014001"},
        intent_source="llm",
        intent_confidence=0.9,
        user_message_id=m[0],
    )
    assert out[0].op == "add"
    await svc.capture_slots(
        app,
        cid,
        {"order_no": "B20261015002"},
        intent_source="llm",
        intent_confidence=0.9,
        user_message_id=m[2],
    )
    # Compaction later reads turn 0 (old value) and a retract from turn 1: both stale.
    out = await svc.write_facts(
        app,
        cid,
        [
            c("order_no", "A20261014001", m[0], "compaction"),
            c("product", "蓝牙耳机 X2", m[1], "compaction"),
        ],
    )
    assert {o.key: o.reason for o in out} == {"order_no": "stale", "product": ""}
    async with pg["sessions"]() as s:
        st = PgConversationStore(s)
        f = await st.get_fact(app, cid, "order_no")
        assert f.value == "B20261015002" and f.version == 2
        hist = await st.list_fact_history(app, cid, key="order_no")
        assert [(h.op, h.old_value, h.new_value) for h in hist] == [
            ("add", None, "A20261014001"),
            ("update", "A20261014001", "B20261015002"),
        ]
    with pytest.raises(ConversationNotFoundError):
        await svc.write_facts(pg["app_b"], cid, [c("order_no", "X12345678", None)])


async def test_concurrent_writers_do_not_lose_updates(pg: dict[str, Any]) -> None:
    """Slot capture (API) and compaction (worker) writing the same keys concurrently."""
    cid, m = await conv_with(pg, 12)
    svc = MemoryService(pg_units(pg["sessions"]))
    app = pg["app_a"]
    # Ten concurrent writers on a key that does not exist yet: exactly one ADD.
    values = [f"ORDER{i:08d}" for i in range(10)]
    results = await asyncio.gather(
        *(svc.write_facts(app, cid, [c("order_no", v, m[i])]) for i, v in enumerate(values))
    )
    ops = [r[0].op for r in results]
    assert ops.count("add") == 1
    assert all(op in ("add", "update", "skip") for op in ops)
    async with pg["sessions"]() as s:
        st = PgConversationStore(s)
        f = await st.get_fact(app, cid, "order_no")
        hist = await st.list_fact_history(app, cid, key="order_no")
    applied = [r[0] for r in results if r[0].op in ("add", "update")]
    # Every applied change has exactly one history row and versions are contiguous.
    assert len(hist) == len(applied) == f.version
    assert [h.op for h in hist][0] == "add"
    for a, b in zip(hist, hist[1:], strict=False):
        assert b.old_value == a.new_value
    assert f.value == hist[-1].new_value
    # Skips may only be "stale" (an older message lost to a newer one), never a lost race.
    assert all(r[0].reason == "stale" for r in results if r[0].op == "skip")
    # The newest message's value is current regardless of commit order.
    assert f.value == values[9]
