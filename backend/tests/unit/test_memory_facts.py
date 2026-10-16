"""M2.5 ②: fact reconciliation (对话上下文管理 §5.3, §7.6)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aics.core import metrics
from aics.memory.facts import (
    FactCandidate,
    normalize_value,
    reconcile,
    same_value,
    slot_candidates,
)
from aics.memory.service import MemoryService, in_memory_units
from aics.memory.store import InMemoryConversationStore

APP = uuid.uuid4()


def setup() -> tuple[InMemoryConversationStore, Any]:
    store = InMemoryConversationStore()
    return store, store.create_conversation(APP)


def msg(store: InMemoryConversationStore, cid: Any, text: str = "m") -> Any:
    return store.add_message(APP, cid, "user", text).id


def cand(
    key: str,
    value: str | None,
    conf: float = 0.9,
    src: str = "slot",
    mid: Any = None,
    retract: bool = False,
) -> FactCandidate:
    return FactCandidate(key, value, conf, src, mid, retract)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("order_no", " Ａ2026 1014 001 ", "A20261014001"),
        ("phone", "+86 138-1234-5678", "13812345678"),
        ("phone", "86 13812345678", "13812345678"),
        ("product", "  X5   空气净化器 ", "X5 空气净化器"),
        ("address_region", "浙江省　杭州市", "浙江省 杭州市"),
    ],
)
def test_normalize(key: str, raw: str, expected: str) -> None:
    assert normalize_value(key, raw) == expected


def test_same_value_is_case_and_space_insensitive() -> None:
    assert same_value("order_no", "a2026-001", "A2026-001")
    assert same_value("product", "X5净化器", "x5 净化器")
    assert not same_value("order_no", "A1", "A2")


async def test_add_update_noop_retract_revive_with_history() -> None:
    store, cid = setup()
    m1, m2, m3, m4, m5 = (msg(store, cid) for _ in range(5))
    out = await reconcile(store, APP, cid, [cand("order_no", "A20261014001", mid=m1)])
    assert out[0].op == "add"
    out = await reconcile(store, APP, cid, [cand("order_no", "a2026 1014 001", 0.95, mid=m2)])
    assert out[0].op == "noop"
    f = await store.get_fact(APP, cid, "order_no")
    assert f.version == 1 and f.confidence == 0.95  # NOOP keeps higher confidence
    out = await reconcile(store, APP, cid, [cand("order_no", "B20261015002", mid=m3)])
    assert out[0].op == "update"
    out = await reconcile(
        store, APP, cid, [cand("order_no", None, src="compaction", mid=m4, retract=True)]
    )
    assert out[0].op == "retract"
    assert await store.list_facts(APP, cid) == []
    out = await reconcile(store, APP, cid, [cand("order_no", "B20261015002", mid=m5)])
    assert out[0].op == "update" and out[0].reason == "revive"
    hist = await store.list_fact_history(APP, cid, key="order_no")
    assert [(h.op, h.old_value, h.new_value) for h in hist] == [
        ("add", None, "A20261014001"),
        ("update", "A20261014001", "B20261015002"),
        ("retract", "B20261015002", None),
        ("update", "B20261015002", "B20261015002"),
    ]
    assert (await store.get_fact(APP, cid, "order_no")).version == 4


async def test_guards_drop_invalid_candidates() -> None:
    store, cid = setup()
    out = await reconcile(
        store,
        APP,
        cid,
        [
            cand("order_no", "A1", conf=0.4),
            cand("password", "x"),
            cand("product", "  "),
            cand("product", "长" * 201),
            cand("phone", None, retract=True),
        ],
    )
    assert [(o.op, o.reason) for o in out] == [
        ("skip", "low_confidence"),
        ("skip", "bad_key"),
        ("skip", "empty_value"),
        ("skip", "too_long"),
        ("skip", "nothing_to_retract"),
    ]
    assert await store.list_facts(APP, cid, include_retracted=True) == []


async def test_batch_conflict_newest_message_then_confidence() -> None:
    store, cid = setup()
    m1, m2 = msg(store, cid), msg(store, cid)
    out = await reconcile(
        store,
        APP,
        cid,
        [
            cand("order_no", "NEW0000002", 0.6, "slot", m2),
            cand("order_no", "OLD0000001", 0.99, "compaction", m1),
            cand("product", "耳机", 0.7, "slot", m2),
            cand("product", "蓝牙耳机 X2", 0.9, "compaction", m2),
        ],
    )
    facts = {f.key: f.value for f in await store.list_facts(APP, cid)}
    assert facts == {"order_no": "NEW0000002", "product": "蓝牙耳机 X2"}
    assert sum(o.reason == "superseded_in_batch" for o in out) == 2


async def test_stale_compaction_does_not_roll_back_newer_slot() -> None:
    """User corrects A → B (slot at turn 5); compaction later processes turn 2 saying A."""
    store, cid = setup()
    m2 = msg(store, cid, "订单号 A20261014001")
    m5 = msg(store, cid, "不对，是 B20261015002")
    await reconcile(store, APP, cid, [cand("order_no", "B20261015002", mid=m5)])
    out = await reconcile(
        store, APP, cid, [cand("order_no", "A20261014001", src="compaction", mid=m2)]
    )
    assert out[0].op == "skip" and out[0].reason == "stale"
    out = await reconcile(
        store, APP, cid, [cand("order_no", None, src="compaction", mid=m2, retract=True)]
    )
    assert out[0].reason == "stale"
    assert (await store.get_fact(APP, cid, "order_no")).value == "B20261015002"


async def test_same_message_lower_confidence_does_not_override() -> None:
    store, cid = setup()
    m = msg(store, cid)
    await reconcile(store, APP, cid, [cand("product", "蓝牙耳机 X2", 0.9, "compaction", m)])
    out = await reconcile(store, APP, cid, [cand("product", "耳机", 0.6, "slot", m)])
    assert out[0].reason == "lower_confidence_same_message"


def test_slot_candidates_filter() -> None:
    mid = uuid.uuid4()
    slots = {"order_no": "A1", "product": " ", "phone": None, "address": "杭州"}
    got = slot_candidates(slots, intent_source="llm", intent_confidence=0.8, user_message_id=mid)
    assert [(c.key, c.value, c.confidence, c.source) for c in got] == [
        ("order_no", "A1", 0.8, "slot")
    ]
    assert (
        slot_candidates(slots, intent_source="rule", intent_confidence=1, user_message_id=mid) == []
    )
    assert (
        slot_candidates(slots, intent_source="llm", intent_confidence=0.4, user_message_id=mid)
        == []
    )


async def test_capture_slots_visible_next_turn_and_metrics() -> None:
    metrics.reset()
    store, cid = setup()
    svc = MemoryService(in_memory_units(store))
    m = msg(store, cid, "我的订单号是 2026101400123")
    out = await svc.capture_slots(
        APP,
        cid,
        {"order_no": "2026101400123"},
        intent_source="llm",
        intent_confidence=0.9,
        user_message_id=m,
    )
    assert [o.op for o in out] == ["add"]
    from aics.memory.context import ContextQuery

    ctx = await svc.load_context(
        ContextQuery(
            app_id=APP, conversation_id=cid, question="到哪了", budget=3000, keep_recent_turns=2
        )
    )
    assert ctx.facts == {"order_no": "2026101400123"}
    assert metrics.value("aics_fact_ops_total", op="add", source="slot") == 1
    assert (
        await svc.capture_slots(
            APP,
            cid,
            {"order_no": "X"},
            intent_source="llm",
            intent_confidence=0.9,
            user_message_id=m,
            enable_facts=False,
        )
        == []
    )
