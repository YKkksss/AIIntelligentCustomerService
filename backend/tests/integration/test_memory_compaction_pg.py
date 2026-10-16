"""M2.5 ③ (integration): compaction on PostgreSQL with the Redis lock.

Covers: 30-turn conversation triggers compaction (version increments, coverage), failure
leaves everything unchanged, two concurrent compactions write once (lock path and
optimistic-version path), app isolation.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
import redis.asyncio as aioredis
from tests.memory_support import FakeMemoryLLM

from aics.memory.compactor import Compactor, CompactParams
from aics.memory.context import ContextQuery
from aics.memory.locks import LocalLock, RedisLock
from aics.memory.repository import PgConversationStore
from aics.memory.service import MemoryService, pg_units
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage

pytestmark = pytest.mark.integration
P = CompactParams(keep_recent_turns=4, compact_trigger_tokens=1500)
REDIS_URL = os.environ.get("AICS_TEST_REDIS_URL", "redis://127.0.0.1:6379/7")


@pytest.fixture
async def redis_client() -> Any:
    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        pytest.skip("redis not reachable")
    yield client
    await client.aclose()


async def long_conversation(pg: dict[str, Any], turns: int = 30) -> Any:
    async with pg["sessions"]() as s:
        conv = await ConversationRepository(s).create(pg["app_a"], NewConversation(channel="api"))
        msgs = MessageRepository(s)
        for i in range(turns):
            extra = " 我的订单号是 A2026101400" + str(i % 10) if i in (3, 11) else ""
            await msgs.append(
                pg["app_a"],
                conv.id,
                NewMessage(role="user", content=f"第{i}轮问题{extra}" + "细节" * 30),
            )
            await msgs.append(
                pg["app_a"],
                conv.id,
                NewMessage(role="assistant", content="客服回复" * 30, answer_type="answer"),
            )
        await s.commit()
        return conv.id


async def test_thirty_turns_compaction_pg(pg: dict[str, Any], redis_client: Any) -> None:
    cid = await long_conversation(pg)
    units = pg_units(pg["sessions"])
    comp = Compactor(units, FakeMemoryLLM(), RedisLock(redis_client, "aics:test:"))
    svc = MemoryService(units, compactor=comp)
    trig, res = await svc.maybe_compact(pg["app_a"], cid, P, run_mode="eval")
    assert trig == "ran" and res.result == "success" and res.version >= 1
    async with pg["sessions"]() as s:
        st = PgConversationStore(s)
        state = await st.get_summary_state(pg["app_a"], cid)
        assert state.version == res.version and state.until_msg_id == res.until_msg_id
        assert len(await st.turns_after(pg["app_a"], cid, state.until_msg_id)) == 4
        f = await st.get_fact(pg["app_a"], cid, "order_no")
        assert f.value == "A20261014001" and f.source == "compaction"
    assert await redis_client.get(f"aics:test:lock:compact:{cid}") is None  # released
    ctx = await svc.load_context(
        ContextQuery(
            app_id=pg["app_a"], conversation_id=cid, question="q", budget=3000, keep_recent_turns=4
        )
    )
    assert ctx.summary is not None and ctx.recent_turns == 4 and "history_gap" not in ctx.flags
    # App isolation: another app cannot compact or read this conversation.
    from aics.core.errors import ConversationNotFoundError

    with pytest.raises(ConversationNotFoundError):
        await svc.compact_now(pg["app_b"], cid, P)


async def test_failure_leaves_state_unchanged_pg(pg: dict[str, Any]) -> None:
    cid = await long_conversation(pg)
    units = pg_units(pg["sessions"])
    comp = Compactor(units, FakeMemoryLLM(lambda p, o: "not json"), LocalLock())
    res = await MemoryService(units, compactor=comp).compact_now(pg["app_a"], cid, P)
    assert res.result == "failed"
    async with pg["sessions"]() as s:
        st = PgConversationStore(s)
        state = await st.get_summary_state(pg["app_a"], cid)
        assert state.version == 0 and state.summary is None
        assert await st.list_facts(pg["app_a"], cid, include_retracted=True) == []
        assert len(await st.turns_after(pg["app_a"], cid, None)) == 30


async def test_concurrent_compactions_with_redis_lock(
    pg: dict[str, Any], redis_client: Any
) -> None:
    cid = await long_conversation(pg)
    units = pg_units(pg["sessions"])
    slow = FakeMemoryLLM()

    async def wait(_: str) -> None:
        await asyncio.sleep(0.05)

    slow.before_reply = wait
    a = Compactor(units, slow, RedisLock(redis_client, "aics:test:"))
    b = Compactor(units, FakeMemoryLLM(), RedisLock(redis_client, "aics:test:"))
    ra, rb = await asyncio.gather(a.run(pg["app_a"], cid, P), b.run(pg["app_a"], cid, P))
    assert sorted([ra.result, rb.result]) == ["skipped_lock", "success"]


async def test_concurrent_compactions_without_lock_version_conflict(pg: dict[str, Any]) -> None:
    """Locks lost (TTL expiry, Redis failover): summary_version still guarantees one write."""
    cid = await long_conversation(pg)
    units = pg_units(pg["sessions"])
    llm_a, llm_b = FakeMemoryLLM(), FakeMemoryLLM()
    gate = asyncio.Event()

    async def hold(prompt: str) -> None:  # both read version 0 before either writes
        if "<turns>" in prompt:
            if gate.is_set():
                return
            await asyncio.sleep(0.05)
            gate.set()

    llm_a.before_reply = llm_b.before_reply = hold
    a, b = Compactor(units, llm_a, LocalLock()), Compactor(units, llm_b, LocalLock())
    ra, rb = await asyncio.gather(a.run(pg["app_a"], cid, P), b.run(pg["app_a"], cid, P))
    results = sorted([ra.result, rb.result])
    assert "success" in results
    async with pg["sessions"]() as s:
        st = PgConversationStore(s)
        state = await st.get_summary_state(pg["app_a"], cid)
        hist = await st.list_fact_history(pg["app_a"], cid)
    if results == ["success", "version_conflict"]:
        winner = ra if ra.result == "success" else rb
        assert state.version == winner.version
    # Facts were written by exactly one run per batch: one add, no duplicate history.
    assert [h.op for h in hist].count("add") == 1
