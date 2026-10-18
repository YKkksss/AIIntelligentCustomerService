"""M2.5 ①: MemoryService through the rag MemoryPort adapter, end to end in the graph."""

from __future__ import annotations

from aics.core import metrics
from aics.core.masking import mask_fact, mask_order, mask_phone, mask_text
from aics.core.tokens import count_tokens, truncate_tokens
from aics.memory.schemas import FactWrite
from aics.memory.service import MemoryService, in_memory_units
from aics.memory.store import InMemoryConversationStore
from aics.modules.chat.memory_port import MemoryPortAdapter
from aics.rag.graph import run_rag_collect
from aics.rag.ports import ChatTurn, MemoryPort
from tests.rag_support import FakeLLMProvider
from tests.unit.test_rag_graph import APP, inp, kb_with, make_responder, ports


async def test_graph_uses_memory_history_facts_and_traces_masked() -> None:
    store = InMemoryConversationStore()
    cid = store.create_conversation(APP)
    store.add_message(APP, cid, "user", "我的订单 A20261014001 的耳机有杂音")
    store.add_message(APP, cid, "assistant", "签收 7 天内可以申请换货。")
    await store.add_fact(
        APP, cid, FactWrite(key="order_no", value="A20261014001", source="slot", confidence=0.9)
    )
    await store.add_fact(
        APP, cid, FactWrite(key="phone", value="13812345678", source="slot", confidence=0.9)
    )
    current = store.add_message(APP, cid, "user", "那运费谁出")
    adapter = MemoryPortAdapter(MemoryService(in_memory_units(store)))
    assert isinstance(adapter, MemoryPort)
    llm = FakeLLMProvider(make_responder())
    res, _ = await run_rag_collect(
        inp("那运费谁出", conversation_id=cid, user_message_id=current.id),
        ports(kb_with({}), llm, memory=adapter),
    )
    cls = next(c for c in llm.calls if c["node"] == "classify")
    prompt = str(cls["messages"][0].content)
    assert "[user] 我的订单 A20261014001 的耳机有杂音" in prompt
    assert "order_no=A20261014001" in prompt and "那运费谁出" in prompt
    assert "[user] 那运费谁出" not in prompt  # current message not duplicated in history
    ctx = res.trace["context"]
    assert ctx["facts"] == {"order_no": "********4001", "phone": "138****5678"}
    assert ctx["recent_turns"] == 1 and ctx["usage"]["budget"] == 3000
    assert res.context_usage["recent"] > 0


async def test_stateless_inline_history_trimmed_by_budget() -> None:
    adapter = MemoryPortAdapter(MemoryService(in_memory_units(InMemoryConversationStore())))
    hist = []
    for i in range(30):
        hist += [
            ChatTurn(role="user", content=f"问题{i}" * 40),
            ChatTurn(role="assistant", content="答" * 200),
        ]
    llm = FakeLLMProvider(make_responder())
    from aics.rag.config import AppRagConfig

    cfg = AppRagConfig(history_token_budget=1000, openai_temp_summary=False)
    res, _ = await run_rag_collect(
        inp("几天到账", inline_history=hist, config=cfg), ports(kb_with({}), llm, memory=adapter)
    )
    assert "history_gap" in res.degraded
    assert res.trace["context"]["recent_turns"] < 30


async def test_empty_inline_history_still_wins() -> None:
    """M3.3 ②：空历史列表也必须走请求内路径，否则会去读服务端会话（OpenAI 兼容不上送历史）。"""
    store = InMemoryConversationStore()
    cid = store.create_conversation(APP)
    store.add_message(APP, cid, "user", "服务端问题")
    store.add_message(APP, cid, "assistant", "服务端回答")
    adapter = MemoryPortAdapter(MemoryService(in_memory_units(store)))
    llm = FakeLLMProvider(make_responder())
    from aics.rag.config import AppRagConfig

    res, _ = await run_rag_collect(
        inp("几天到账", conversation_id=cid, inline_history=[], config=AppRagConfig()),
        ports(kb_with({}), llm, memory=adapter),
    )
    assert res.trace["context"]["recent_turns"] == 0


def test_masking_and_tokens_and_metrics() -> None:
    assert mask_phone("13812345678") == "138****5678"
    assert mask_order("2026101400123") == "*********0123"
    assert mask_fact("product", "X5") == "X5" and mask_fact("phone", "13812345678")[:3] == "138"
    assert mask_text("电话13812345678，谢谢") == "电话138****5678，谢谢"
    assert (
        truncate_tokens("你好世界" * 100, 10)
        and count_tokens(truncate_tokens("你好" * 100, 7)) <= 7
    )
    assert truncate_tokens("短", 10) == "短" and truncate_tokens("x", 0) == ""
    metrics.reset()
    metrics.inc("aics_x_total", result="ok")
    metrics.inc("aics_x_total", result="ok")
    metrics.inc("aics_x_total", result="failed")
    assert metrics.value("aics_x_total", result="ok") == 2 and metrics.value("aics_x_total") == 3
    metrics.observe("aics_h", 1.5)
    assert metrics.observations("aics_h") == [1.5]
