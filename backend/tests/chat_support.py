"""Shared fakes for chat.service / chat API tests: a keyword knowledge base, a scripted LLM
and a ChatRuntime over real PG (memory included, compaction enqueue recorded)."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from langchain_core.messages import BaseMessage

from aics.memory.service import MemoryService
from aics.memory.units import pg_units
from aics.modules.chat.runtime import ChatRuntime
from tests.rag_support import FakeKB, FakeLLMProvider, KbChunk, classify_json, node_of, tag

KB = [
    KbChunk(uuid.uuid4(), "售后政策", "退货 > 时效", "签收后 7 天内可无理由退货。", ("退货",)),
    KbChunk(uuid.uuid4(), "售后政策", "退款 > 到账", "退款 3–5 个工作日原路退回。", ("退款",)),
]
ANSWER = {"退货": "签收后 7 天内可以无理由退货[1]。", "退款": "退款 3–5 个工作日原路退回[1]。"}
ORDER_RE = re.compile(r"\d{13}")


def responder(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
    text = str(messages[0].content)
    node = node_of(messages)
    if node == "classify":
        q = tag(text, "question")
        slots = {"order_no": m.group(0)} if (m := ORDER_RE.search(q)) else {}
        if "刚才给你的" in q:
            return classify_json(q, "recall", 0.95)
        if "随便聊" in q:
            return classify_json(q, "chitchat", 0.95)
        if "那个怎么弄" in q:
            return classify_json(
                q, "clarify", 0.8, need_clarify=True, clarify_question="您想问退货还是退款？"
            )
        return classify_json(q, "kb_qa", 0.9, slots=slots)
    if node == "judge":
        return json.dumps({"answerable": True, "reason": "ok"})
    if node == "chitchat":
        return "很高兴为您服务。"
    if node == "recall":
        m = ORDER_RE.search(tag(text, "facts"))
        return f"您提供的订单号是{m.group(0)}。" if m else "没有找到。"
    ctx = text
    for k, v in ANSWER.items():
        if k in ctx:
            return v
    return "抱歉[1]。"


class Enqueued(list[tuple[Any, ...]]):
    def __call__(self, *args: Any) -> None:
        self.append(args)


def make_runtime(sessions: Any, *, llm: FakeLLMProvider | None = None) -> ChatRuntime:
    kb = FakeKB(KB)
    enq = Enqueued()
    memory = MemoryService(pg_units(sessions), enqueue_compaction=enq)
    rt = ChatRuntime(
        sessions=sessions,
        chunk_search=kb,
        vector=kb,
        reranker=kb,
        llm=llm or FakeLLMProvider(responder),
        memory=memory,
    )
    rt.enqueued = enq  # type: ignore[attr-defined]
    return rt
