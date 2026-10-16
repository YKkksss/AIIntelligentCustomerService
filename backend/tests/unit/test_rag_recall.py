"""M2.6 ④: recall intent — answer from conversation memory (设计讨论 0007)."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import BaseMessage

from aics.rag.config import AppRagConfig, IntentConfig
from aics.rag.graph import run_rag_collect
from aics.rag.llm import LlmCallError, reset_breakers
from aics.rag.nodes.replies import RECALL_NOT_FOUND
from aics.rag.ports import ChatTurn, ContextBundle, ContextRequest
from tests.rag_support import FakeLLMProvider, classify_json, node_of, tag
from tests.unit.test_rag_graph import inp, kb_with, make_responder, ports

Q = "我刚才给你的订单号是多少"
ORDER = "2026101400123"


@pytest.fixture(autouse=True)
def _fresh() -> None:
    reset_breakers()


class Mem:
    def __init__(self, facts: dict[str, str] | None = None, history: list[ChatTurn] | None = None):
        self.facts = facts or {}
        self.history = history or []

    async def load_context(self, req: ContextRequest) -> ContextBundle:
        return ContextBundle(history=self.history, facts=self.facts, usage={})


def responder(recall: Any) -> Any:
    base = make_responder(classify=lambda q: classify_json(q, "recall", 0.95))

    def respond(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        if node_of(messages) == "recall":
            return recall(messages, kwargs) if callable(recall) else recall
        return base(messages, kwargs)

    return respond


HIST = [
    ChatTurn(role="user", content=f"我的订单号是{ORDER}"),
    ChatTurn(role="assistant", content="好的，已记下。"),
]


async def test_recall_answers_from_memory_without_retrieval() -> None:
    llm = FakeLLMProvider(responder(f"您刚才提供的订单号是{ORDER}。"))
    kb = kb_with({})
    mem = Mem({"order_no": ORDER}, HIST)
    res, events = await run_rag_collect(inp(Q), ports(kb, llm, memory=mem))
    assert res.intent.name == "recall" and res.decision_reason == "intent:recall"
    assert res.answer_type == "answer" and ORDER in res.answer and res.citations == []
    assert res.prompt_versions["recall"] == "1"
    assert kb.search_calls == [] and "identifier_mismatch" not in res.degraded
    call = next(c for c in llm.calls if c["node"] == "recall")
    text = str(call["messages"][0].content)
    assert ORDER in tag(text, "facts") and Q in tag(text, "question")
    assert any(e.kind == "delta" for e in events)


async def test_recall_empty_memory_skips_llm() -> None:
    llm = FakeLLMProvider(responder("不该被调用"))
    res, _ = await run_rag_collect(inp(Q), ports(kb_with({}), llm, memory=Mem()))
    assert res.answer == RECALL_NOT_FOUND and res.decision_reason == "intent:recall(empty)"
    assert not any(c["node"] == "recall" for c in llm.calls)


async def test_recall_llm_failure_falls_back_to_facts() -> None:
    def broken(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        raise LlmCallError("down")

    llm = FakeLLMProvider(responder(broken))
    res, _ = await run_rag_collect(
        inp(Q), ports(kb_with({}), llm, memory=Mem({"order_no": ORDER}, HIST))
    )
    assert "recall_llm_failed" in res.degraded
    assert res.answer == f"根据本次对话记录：订单号：{ORDER}。"


async def test_recall_llm_failure_without_known_facts() -> None:
    def broken(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        raise LlmCallError("down")

    llm = FakeLLMProvider(responder(broken))
    res, _ = await run_rag_collect(inp(Q), ports(kb_with({}), llm, memory=Mem(history=HIST)))
    assert res.answer == RECALL_NOT_FOUND


async def test_recall_invented_identifier_is_flagged() -> None:
    llm = FakeLLMProvider(responder("您的订单号是9999888877776。"))
    res, _ = await run_rag_collect(
        inp(Q), ports(kb_with({}), llm, memory=Mem({"order_no": ORDER}, HIST))
    )
    assert "identifier_mismatch" in res.degraded
    assert res.trace["recall"]["identifiers"]["mismatched"]


async def test_recall_identifier_from_history_only_is_ok() -> None:
    phone = "13800138000"
    hist = [ChatTurn(role="user", content=f"我手机{phone}")]
    llm = FakeLLMProvider(responder(f"您提供的手机号是{phone}。"))
    res, _ = await run_rag_collect(inp(Q), ports(kb_with({}), llm, memory=Mem(history=hist)))
    assert "identifier_mismatch" not in res.degraded


async def test_recall_disabled_falls_back_to_kb_qa() -> None:
    cfg = AppRagConfig(
        intent=IntentConfig(enabled_intents=["kb_qa", "chitchat", "human", "out_of_scope"])
    )
    llm = FakeLLMProvider(responder("x"))
    res, _ = await run_rag_collect(
        inp(Q, config=cfg), ports(kb_with({}), llm, memory=Mem({"order_no": ORDER}, HIST))
    )
    assert res.intent.name == "kb_qa"
    assert not any(c["node"] == "recall" for c in llm.calls)
