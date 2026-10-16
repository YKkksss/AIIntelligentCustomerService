"""M2.3 (integration): run_rag over the real ingest pipeline + PG chunk search + in-memory
Qdrant, with a FakeLLM. Answerable → cited answer with PG document titles; unanswerable →
refused; rag reads PG only through RagPorts."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from test_retrieval_pg import kb  # noqa: F401  (fixture re-export)
from tests.rag_support import FakeLLMProvider, classify_json, node_of, tag

from aics.rag.graph import run_rag_collect
from aics.rag.ports import RagPorts
from aics.rag.schemas import AppProfile, RagInput

pytestmark = pytest.mark.integration
PROFILE = AppProfile(name="测试商城", fallback_text="抱歉，暂时无法回答。")


class QueryAwareReranker:
    """1.0 for the freight chunk when the query is about freight, else 0.1."""

    model = "x"

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        hit = "运费" in query
        s = [(i, 1.0 if hit and "运费由买家承担" in d else 0.1) for i, d in enumerate(docs)]
        return sorted(s, key=lambda x: -x[1])[:top_n]


def responder(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
    text = str(messages[0].content)
    node = node_of(messages)
    if node == "classify":
        return classify_json(tag(text, "question"))
    if node == "judge":
        return json.dumps({"answerable": "运费" in tag(text, "context")})
    return "非质量问题退货运费由买家承担[1]。"


async def test_answerable_and_unanswerable(kb: dict[str, Any]) -> None:  # noqa: F811
    llm = FakeLLMProvider(responder)
    ports = RagPorts(
        chunk_search=kb["chunks"],
        vector=kb["vector"],
        reranker=QueryAwareReranker(),
        llm=llm,
    )
    res, _ = await run_rag_collect(
        RagInput(app_id=kb["app"], question="非质量问题退货运费谁承担？", profile=PROFILE), ports
    )
    assert res.answer_type == "answer"
    assert res.citations and res.citations[0].document_title
    assert res.decision_reason == "above_gray_upper"
    assert res.trace["retrieval"]["reranked"]

    res2, _ = await run_rag_collect(
        RagInput(app_id=kb["app"], question="公司股票代码是多少", profile=PROFILE), ports
    )
    assert res2.answer_type == "refused" and res2.answer == PROFILE.fallback_text
    assert res2.citations == [] and res2.decision_reason == "below_threshold"
