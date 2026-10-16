"""M2.5 ①: load_context assembly (对话上下文管理 §6.2, §10)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aics.core.tokens import count_tokens, truncate_tokens
from aics.memory.context import (
    FACTS_MAX_TOKENS,
    MESSAGE_TRUNCATE_TOKENS,
    REFUSED_PLACEHOLDER,
    ContextQuery,
    HistoryMessage,
    select_facts,
)
from aics.memory.schemas import ConversationSummary, FactWrite
from aics.memory.service import MemoryService, in_memory_units
from aics.memory.store import InMemoryConversationStore

APP = uuid.uuid4()


def setup(turns: list[tuple[str, str]] | None = None) -> tuple[InMemoryConversationStore, Any]:
    store = InMemoryConversationStore()
    cid = store.create_conversation(APP)
    for u, a in turns or []:
        store.add_message(APP, cid, "user", u)
        store.add_message(APP, cid, "assistant", a)
    return store, cid


def q(cid: Any, budget: int = 3000, keep: int = 2, **kw: Any) -> ContextQuery:
    return ContextQuery(
        app_id=APP,
        conversation_id=cid,
        question="现在的问题",
        budget=budget,
        keep_recent_turns=keep,
        **kw,
    )


async def load(store: InMemoryConversationStore, query: ContextQuery) -> Any:
    return await MemoryService(in_memory_units(store)).load_context(query)


async def test_empty_history() -> None:
    store, cid = setup()
    ctx = await load(store, q(cid))
    assert ctx.history == [] and ctx.facts == {} and ctx.summary is None
    assert ctx.flags == [] and ctx.usage == {
        "facts": 0,
        "summary": 0,
        "recent": 0,
        "recall": 0,
        "budget": 3000,
    }


async def test_all_turns_fit_in_order() -> None:
    store, cid = setup([(f"问{i}", f"答{i}") for i in range(5)])
    ctx = await load(store, q(cid))
    assert [m.content for m in ctx.history] == [x for i in range(5) for x in (f"问{i}", f"答{i}")]
    assert ctx.recent_turns == 5 and ctx.flags == []
    assert ctx.usage["recent"] == sum(count_tokens(m.content) for m in ctx.history)


async def test_budget_drops_oldest_and_flags_gap() -> None:
    long = "退货政策说明" * 40  # ~ 240 tokens
    store, cid = setup([(f"问{i}", long) for i in range(10)])
    ctx = await load(store, q(cid, budget=1000, keep=1))
    assert 1 <= ctx.recent_turns < 10
    assert ctx.history[-2].content == "问9"  # newest kept, oldest dropped
    assert "history_gap" in ctx.flags and ctx.usage["recent"] <= 1000


async def test_floor_keeps_recent_turns_beyond_budget_and_truncates_long_message() -> None:
    huge = "很长的回答内容" * 400  # > 500 tokens
    store, cid = setup([("早期" * 150, "答"), ("问", huge)])
    ctx = await load(store, q(cid, budget=600, keep=1))
    assert ctx.recent_turns == 1 and "truncated" in ctx.flags and "history_gap" in ctx.flags
    assert count_tokens(ctx.history[-1].content) <= MESSAGE_TRUNCATE_TOKENS
    assert ctx.history[-1].content == truncate_tokens(huge, MESSAGE_TRUNCATE_TOKENS)


async def test_floor_without_truncation_when_short_but_over_budget() -> None:
    store, cid = setup([("问一" * 30, "答一" * 30), ("问二" * 30, "答二" * 30)])
    ctx = await load(store, q(cid, budget=10, keep=2))
    assert ctx.recent_turns == 2 and "truncated" not in ctx.flags and "history_gap" not in ctx.flags


async def test_summary_counts_against_budget_and_limits_recent() -> None:
    store, cid = setup([(f"问{i}", "答" * 50) for i in range(6)])
    turns = await store.turns_after(APP, cid, None)
    until = turns[3].last_id  # summary covers the first 3 turns
    s = ConversationSummary(user_goal="退货", confirmed_info=["订单 A123456789"])
    assert await store.save_summary(APP, cid, s, until, 0)
    ctx = await load(store, q(cid, budget=5000, keep=1))
    assert ctx.summary == s.model_dump() and ctx.summary_version == 1
    assert [m.content for m in ctx.history][::2] == ["问3", "问4", "问5"]  # only uncovered
    assert ctx.usage["summary"] > 0


async def test_facts_injected_by_importance_and_capped() -> None:
    store, cid = setup([("hi", "hello")])
    for k, v in [("user_emotion", "着急"), ("order_no", "A20261014001"), ("product", "X5 净化器")]:
        await store.add_fact(APP, cid, FactWrite(key=k, value=v, source="slot", confidence=0.9))
    ctx = await load(store, q(cid))
    assert list(ctx.facts) == ["order_no", "product", "user_emotion"]
    ctx = await load(store, q(cid, enable_facts=False))
    assert ctx.facts == {} and ctx.usage["facts"] == 0


async def test_retracted_facts_not_injected() -> None:
    store, cid = setup([("hi", "hello")])
    f = await store.add_fact(
        APP, cid, FactWrite(key="order_no", value="A123456789", source="slot", confidence=0.9)
    )
    await store.retract_fact(
        APP,
        cid,
        "order_no",
        source="compaction",
        confidence=0.9,
        source_message_id=None,
        expected_version=f.version,
    )
    assert (await load(store, q(cid))).facts == {}


async def test_select_facts_cap() -> None:
    store, cid = setup()
    for i in range(26):
        await store.add_fact(
            APP,
            cid,
            FactWrite(
                key="other:k" + "abcdefghijklmnopqrstuvwxyzabcdefghijklmn"[i],
                value="很长的事实值" * 5,
                source="slot",
                confidence=0.9,
            ),
        )
    picked = select_facts(await store.list_facts(APP, cid))
    assert 0 < len(picked) < 26
    from aics.memory.context import facts_tokens

    assert facts_tokens(picked) <= FACTS_MAX_TOKENS


async def test_refused_and_human_agent_rendering_and_system_excluded() -> None:
    store, cid = setup()
    store.add_message(APP, cid, "user", "问")
    store.add_message(
        APP, cid, "assistant", "抱歉，这个问题我暂时无法回答……", answer_type="refused"
    )
    store.add_message(APP, cid, "system", "会话即将关闭")
    store.add_message(APP, cid, "human_agent", "您好，我是人工客服")
    ctx = await load(store, q(cid))
    assert [(m.role, m.content) for m in ctx.history] == [
        ("user", "问"),
        ("assistant", REFUSED_PLACEHOLDER),
        ("human_agent", "您好，我是人工客服"),
    ]


async def test_current_user_message_excluded() -> None:
    store, cid = setup([("问1", "答1")])
    m = store.add_message(APP, cid, "user", "现在的问题")
    ctx = await load(store, q(cid, exclude_message_id=m.id))
    assert [x.content for x in ctx.history] == ["问1", "答1"]
    # Without the id, a trailing lone user message equal to the question is also dropped.
    ctx = await load(store, q(cid))
    assert [x.content for x in ctx.history] == ["问1", "答1"]


async def test_other_app_cannot_read() -> None:
    from aics.core.errors import ConversationNotFoundError

    store, cid = setup([("问", "答")])
    with pytest.raises(ConversationNotFoundError):
        await MemoryService(in_memory_units(store)).load_context(
            ContextQuery(
                app_id=uuid.uuid4(),
                conversation_id=cid,
                question="x",
                budget=100,
                keep_recent_turns=1,
            )
        )


# ---------------------------------------------------------------- stateless (§10)
def inline(n: int, size: int = 1) -> list[HistoryMessage]:
    out: list[HistoryMessage] = []
    for i in range(n):
        out += [
            HistoryMessage("user", f"问{i}" * size),
            HistoryMessage("assistant", f"答{i}" * size),
        ]
    return out


async def test_stateless_trims_with_same_algorithm() -> None:
    svc = MemoryService(in_memory_units(InMemoryConversationStore()))
    ctx = await svc.load_context(
        ContextQuery(
            app_id=APP,
            conversation_id=None,
            question="q",
            budget=300,
            keep_recent_turns=1,
            inline_history=inline(10, 30),
            temp_summary=False,
        )
    )
    assert ctx.recent_turns < 10 and "history_gap" in ctx.flags
    assert ctx.history[-1].content == "答9" * 30


async def test_stateless_temp_summary() -> None:
    seen: list[list[HistoryMessage]] = []

    async def summarize(msgs: list[HistoryMessage]) -> dict[str, Any]:
        seen.append(msgs)
        return {"user_goal": "退货", "confirmed_info": [], "solutions_given": [], "open_issues": []}

    svc = MemoryService(in_memory_units(InMemoryConversationStore()), temp_summarizer=summarize)
    ctx = await svc.load_context(
        ContextQuery(
            app_id=APP,
            conversation_id=None,
            question="q",
            budget=300,
            keep_recent_turns=1,
            inline_history=inline(10, 30),
        )
    )
    assert "temp_summary" in ctx.flags and ctx.summary["user_goal"] == "退货"
    assert seen and seen[0][0].content == "问0" * 30
    assert ctx.usage["summary"] > 0
    # Within budget → no summary call.
    seen.clear()
    ctx = await svc.load_context(
        ContextQuery(
            app_id=APP,
            conversation_id=None,
            question="q",
            budget=3000,
            keep_recent_turns=1,
            inline_history=inline(3),
        )
    )
    assert not seen and ctx.flags == [] and ctx.recent_turns == 3
