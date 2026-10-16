"""Behavioural contract shared by every ConversationStore implementation (M2.1).

Run against InMemoryConversationStore (tests/unit/test_memory_store.py) and
PgConversationStore (tests/integration/test_memory_store_pg.py) so both stay identical.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aics.core.errors import ConversationNotFoundError, ValidationAppError
from aics.core.textsearch import build_tsquery
from aics.core.tokens import count_tokens
from aics.memory.schemas import ConversationSummary, FactWrite
from aics.memory.store import ConversationStore, FactExistsError, FactNotFoundError


@dataclass
class Harness:
    store: ConversationStore
    app_a: UUID
    app_b: UUID
    new_conversation: Callable[[UUID], Awaitable[UUID]]
    # (app_id, conversation_id, role, content, **extra) -> message id
    add_message: Callable[..., Awaitable[UUID]]


def fw(key: str = "order_no", value: str = "A123", **kw: Any) -> FactWrite:
    return FactWrite(
        key=key,
        value=value,
        source=kw.pop("source", "slot"),
        confidence=kw.pop("confidence", 0.9),
        **kw,
    )


async def check_turns_and_tokens(h: Harness) -> None:
    a = h.app_a
    c = await h.new_conversation(a)
    assert await h.store.turns_after(a, c, None) == []
    u1 = await h.add_message(a, c, "user", "我要退货")
    a1 = await h.add_message(a, c, "assistant", "好的，请提供订单号", answer_type="answer")
    await h.add_message(a, c, "system", "即将结束", kind="idle_notice")
    u2 = await h.add_message(a, c, "user", "订单号 A123")
    a2 = await h.add_message(a, c, "assistant", "已收到", answer_type="answer")

    turns = await h.store.turns_after(a, c, None)
    assert [[m.id for m in t.messages] for t in turns] == [[u2, a2], [u1, a1]]  # newest first
    assert turns[1].token_count == count_tokens("我要退货") + count_tokens("好的，请提供订单号")
    after = await h.store.turns_after(a, c, a1)
    assert [[m.id for m in t.messages] for t in after] == [[u2, a2]]
    assert await h.store.turns_after(a, c, a2) == []
    with pytest.raises(ValidationAppError):
        await h.store.turns_after(a, c, uuid.uuid4())


async def check_summary_optimistic_lock(h: Harness) -> None:
    a = h.app_a
    c = await h.new_conversation(a)
    m1 = await h.add_message(a, c, "user", "我要退货")
    st = await h.store.get_summary_state(a, c)
    assert st.summary is None and st.version == 0 and st.until_msg_id is None

    s1 = ConversationSummary(user_goal="退货", confirmed_info=["订单 A123"])
    assert await h.store.save_summary(a, c, s1, m1, expected_version=0) is True
    assert await h.store.save_summary(a, c, s1, m1, expected_version=0) is False  # stale
    st = await h.store.get_summary_state(a, c)
    assert st.version == 1 and st.summary == s1 and st.until_msg_id == m1
    assert st.updated_at is not None
    for v in range(1, 7):
        assert await h.store.save_summary(a, c, s1, m1, expected_version=v)
    assert (await h.store.get_summary_state(a, c)).version == 7
    with pytest.raises(ValidationAppError):  # message of another conversation
        other = await h.new_conversation(a)
        om = await h.add_message(a, other, "user", "hi")
        await h.store.save_summary(a, c, s1, om, expected_version=7)
    with pytest.raises(ValidationError):
        ConversationSummary(user_goal="x" * 201)
    with pytest.raises(ValidationError):
        ConversationSummary(open_issues=["x"] * 9)


async def check_search(h: Harness) -> None:
    a = h.app_a
    c = await h.new_conversation(a)
    m1 = await h.add_message(a, c, "user", "我的订单号是 A123，想申请退货")
    await h.add_message(a, c, "assistant", "退货需要在签收七天内申请", answer_type="answer")
    await h.add_message(a, c, "system", "退货 退货 退货", kind="idle_notice")
    await h.add_message(a, c, "user", "运费谁出")
    tsq = build_tsquery(a, "之前说的退货订单")
    assert tsq
    hits = await h.store.search_messages(a, c, tsq, [], 5)
    assert len(hits) == 2 and all(x.role != "system" for x in hits)
    assert all(x.score > 0 for x in hits)
    hits = await h.store.search_messages(a, c, tsq, [m1], 5)
    assert m1 not in [x.message_id for x in hits]
    assert len(await h.store.search_messages(a, c, tsq, [], 1)) == 1
    assert await h.store.search_messages(a, c, "", [], 3) == []
    with pytest.raises(ValidationAppError):
        await h.store.search_messages(a, c, tsq, [], 0)
    # other app sees nothing
    with pytest.raises(ConversationNotFoundError):
        await h.store.search_messages(h.app_b, c, tsq, [], 3)


async def check_fact_lifecycle(h: Harness) -> None:
    """ADD → UPDATE → RETRACT → revive; history keeps every old value."""
    a = h.app_a
    c = await h.new_conversation(a)
    m1 = await h.add_message(a, c, "user", "订单 A123")
    m2 = await h.add_message(a, c, "user", "不对，是 B456")
    m3 = await h.add_message(a, c, "user", "订单号不用管了")

    f = await h.store.add_fact(a, c, fw(source_message_id=m1))
    assert (f.version, f.category, f.importance, f.active) == (1, "order", 3, True)
    with pytest.raises(FactExistsError):
        await h.store.add_fact(a, c, fw(value="Z"))

    with pytest.raises(ValidationAppError):  # same value is a NOOP, not an update
        await h.store.update_fact(a, c, fw(), expected_version=1)
    assert await h.store.update_fact(a, c, fw(value="B456"), expected_version=5) is None
    f2 = await h.store.update_fact(a, c, fw(value="B456", source_message_id=m2), expected_version=1)
    assert f2 is not None and f2.value == "B456" and f2.version == 2 and f2.id == f.id

    t = await h.store.touch_fact(a, c, "order_no", confidence=0.95)
    assert t is not None and t.version == 2 and t.confidence == pytest.approx(0.95)
    t = await h.store.touch_fact(a, c, "order_no", confidence=0.6)
    assert t is not None and t.confidence == pytest.approx(0.95)  # keeps the higher one
    assert await h.store.touch_fact(a, c, "phone", confidence=0.9) is None

    r = await h.store.retract_fact(
        a,
        c,
        "order_no",
        source="compaction",
        confidence=0.8,
        source_message_id=m3,
        expected_version=2,
    )
    assert r is not None and not r.active and r.version == 3
    again = await h.store.retract_fact(
        a,
        c,
        "order_no",
        source="compaction",
        confidence=0.8,
        source_message_id=m3,
        expected_version=3,
    )
    assert again is None  # already retracted
    assert await h.store.list_facts(a, c) == []
    assert [x.key for x in await h.store.list_facts(a, c, include_retracted=True)] == ["order_no"]

    revived = await h.store.update_fact(
        a, c, fw(value="B456", source="compaction"), expected_version=3
    )
    assert revived is not None and revived.active and revived.version == 4

    hist = await h.store.list_fact_history(a, c)
    assert [(x.op, x.old_value, x.new_value) for x in hist] == [
        ("add", None, "A123"),
        ("update", "A123", "B456"),
        ("retract", "B456", None),
        ("update", "B456", "B456"),
    ]
    assert hist[1].old_source_message_id == m1 and hist[1].new_source_message_id == m2
    assert all(x.fact_id == f.id for x in hist)
    assert await h.store.list_fact_history(a, c, key="phone") == []

    with pytest.raises(FactNotFoundError):
        await h.store.update_fact(a, c, fw(key="phone", value="13800000000"), expected_version=1)
    with pytest.raises(FactNotFoundError):
        await h.store.retract_fact(
            a,
            c,
            "phone",
            source="slot",
            confidence=0.9,
            source_message_id=None,
            expected_version=1,
        )


async def check_fact_order_and_validation(h: Harness) -> None:
    a = h.app_a
    c = await h.new_conversation(a)
    await h.store.add_fact(a, c, fw("user_emotion", "着急"))
    await h.store.add_fact(a, c, fw("product", "蓝牙耳机 X2"))
    await h.store.add_fact(a, c, fw("phone", "13800000000"))
    await h.store.add_fact(a, c, fw("other:delivery_slot", "周末"))
    facts = await h.store.list_facts(a, c)
    assert [x.importance for x in facts] == sorted((x.importance for x in facts), reverse=True)
    assert facts[0].key == "phone"
    assert {x.key: x.category for x in facts}["other:delivery_slot"] == "other"

    for bad in [
        {"key": "password", "value": "x"},
        {"key": "other:Bad", "value": "x"},
        {"key": "order_no", "value": "   "},
        {"key": "order_no", "value": "x" * 201},
        {"key": "order_no", "value": "x", "confidence": 0.3},
        {"key": "order_no", "value": "x", "source": "llm"},
    ]:
        with pytest.raises(ValidationError):
            FactWrite(**({"source": "slot", "confidence": 0.9} | bad))
    assert fw(value="  A123 ").value == "A123"


async def check_message_positions(h: Harness) -> None:
    c = await h.new_conversation(h.app_a)
    ids = [await h.add_message(h.app_a, c, "user", f"m{i}") for i in range(4)]
    pos = await h.store.message_positions(h.app_a, c, [ids[2], ids[0], ids[3], uuid4()])
    assert set(pos) == {ids[0], ids[2], ids[3]}
    assert pos[ids[0]] < pos[ids[2]] < pos[ids[3]]
    other = await h.new_conversation(h.app_a)
    assert await h.store.message_positions(h.app_a, other, ids) == {}


async def check_isolation(h: Harness) -> None:
    a, b = h.app_a, h.app_b
    c = await h.new_conversation(a)
    m = await h.add_message(a, c, "user", "订单 A123")
    await h.store.add_fact(a, c, fw(source_message_id=m))
    for call in (
        h.store.get_summary_state(b, c),
        h.store.turns_after(b, c, None),
        h.store.list_facts(b, c),
        h.store.get_fact(b, c, "order_no"),
        h.store.list_fact_history(b, c),
        h.store.add_fact(b, c, fw("phone", "1")),
        h.store.update_fact(b, c, fw(value="X"), expected_version=1),
        h.store.touch_fact(b, c, "order_no", confidence=1),
        h.store.save_summary(b, c, ConversationSummary(), m, expected_version=0),
    ):
        with pytest.raises(ConversationNotFoundError):
            await call
    with pytest.raises(ConversationNotFoundError):
        await h.store.list_facts(a, uuid.uuid4())
    # app A data unchanged
    assert [x.value for x in await h.store.list_facts(a, c)] == ["A123"]


CONTRACT = [
    check_turns_and_tokens,
    check_summary_optimistic_lock,
    check_search,
    check_fact_lifecycle,
    check_fact_order_and_validation,
    check_message_positions,
    check_isolation,
]
