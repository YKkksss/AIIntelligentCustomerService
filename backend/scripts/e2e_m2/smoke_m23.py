"""M2.3 live smoke: run_rag end to end on 20 sample questions with real models.

Corpus / questions: the M1 main corpus and scripts/e2e_m1/questions.json (same as eval_m22):
12 answerable (spread over categories) + 8 unanswerable. Real DashScope embedding / rerank
(cached), real DeepSeek chat through `run_rag` with a response cache keyed by
(model, overrides, messages, kwargs) so re-runs are free.

Variants: `off` / `on` = `rag_config.llm_thinking` (generate with / without reasoning);
`norerank` = `enable_rerank=false`, so every kb_qa question goes through the LLM judge.
Classify / judge always run without thinking and are shared between variants via the cache.

Checks per question: answerable → answer_type=answer, ≥ 1 citation, and a cited source matches
the expected document + evidence (M1 hit rule); unanswerable → refused (or handoff).
Usage (from backend/, env loaded):
  uv run python -m scripts.e2e_m2.smoke_m23 [--variants off,on] [--scope full|narrow]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.providers import build_embedder, build_llm, build_reranker
from aics.rag.config import AppRagConfig
from aics.rag.graph import run_rag_collect
from aics.rag.ports import RagPorts
from aics.rag.retrieval import EmbeddingVectorSearch
from aics.rag.schemas import AppProfile, RagInput, RagResult
from aics.vectorstore import QdrantVectorStore
from scripts.e2e_m2.eval_m22 import (
    EMBED_PRICE,
    M1,
    RERANK_PRICE,
    RES,
    CachedEmbedder,
    CachedReranker,
    _JsonCache,
    build,
    judge,
)

LLM_PRICE_IN, LLM_PRICE_OUT = 2.0, 8.0  # yuan / 1M tokens, cache-miss peak price (conservative)
N_ANSWERABLE, N_UNANSWERABLE = 12, 8
FALLBACK = "抱歉，这个问题我暂时无法回答，您可以换个问法或联系人工客服。"
# `description` feeds {app_scope} of condense_classify. "narrow" describes only the shop's
# customer-service topics (narrower than the corpus); "full" lists everything in the KB.
SCOPES = {
    "narrow": "星河优选电商平台的售前、售后、物流、会员与支付咨询",
    "full": (
        "星河优选电商平台的售后与交易规则、常见问题、商品手册（空气净化器）、仓储物流合同、"
        "区域运费与时效、星河豆积分规则、SKU 价格、跨境退货、供应商协作，以及平台检索技术研究资料"
    ),
}


class _Spend:
    def __init__(self) -> None:
        self.prompt = self.completion = self.reasoning = self.calls = 0

    @property
    def yuan(self) -> float:
        return (self.prompt * LLM_PRICE_IN + self.completion * LLM_PRICE_OUT) / 1e6


class CachedChat(BaseChatModel):
    """Replays cached replies; on a miss calls the real model and stores text + usage."""

    inner: Any
    key_base: str
    store: Any
    spend: Any

    @property
    def _llm_type(self) -> str:
        return "cached"

    def _key(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        payload = [
            self.key_base,
            [(m.type, m.content) for m in messages],
            {k: v for k, v in kwargs.items() if k != "stream_usage"},
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()

    def _record(self, usage: dict[str, Any]) -> None:
        self.spend.prompt += int(usage.get("input_tokens") or 0)
        self.spend.completion += int(usage.get("output_tokens") or 0)
        self.spend.reasoning += int((usage.get("output_token_details") or {}).get("reasoning") or 0)
        self.spend.calls += 1

    def _generate(self, *a: Any, **k: Any) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        key = self._key(messages, kwargs)
        hit = self.store.data.get(key)
        if hit is None:
            msg = await self.inner.ainvoke(messages, **kwargs)
            hit = {"text": msg.content, "usage": dict(msg.usage_metadata or {})}
            self._record(hit["usage"])
            self.store.data[key] = hit
            self.store.save()
        out = AIMessage(content=hit["text"], usage_metadata=hit["usage"] or None)
        return ChatResult(generations=[ChatGeneration(message=out)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        key = self._key(messages, kwargs)
        hit = self.store.data.get(key)
        if hit is None:
            text, usage, t0, first = "", {}, time.perf_counter(), None
            async for ch in self.inner.astream(messages, **kwargs):
                if ch.content and first is None:
                    first = int((time.perf_counter() - t0) * 1000)
                text += ch.content if isinstance(ch.content, str) else ""
                if ch.usage_metadata:
                    usage = dict(ch.usage_metadata)
                yield ChatGenerationChunk(message=AIMessageChunk(content=ch.content))
            hit = {
                "text": text,
                "usage": usage,
                "first_token_ms": first,
                "total_ms": int((time.perf_counter() - t0) * 1000),
            }
            self._record(usage)
            self.store.data[key] = hit
            self.store.save()
            yield ChatGenerationChunk(
                message=AIMessageChunk(content="", usage_metadata=usage or None)
            )
            return
        yield ChatGenerationChunk(message=AIMessageChunk(content=hit["text"]))
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", usage_metadata=hit["usage"] or None)
        )


class CachedLLM:
    def __init__(self, inner: Any) -> None:
        self.inner, self.default_model = inner, inner.default_model
        self.cache = _JsonCache("llm_m23_cache.json")
        self.spend = _Spend()

    def chat_model(self, **ov: Any) -> CachedChat:
        base = json.dumps({k: v for k, v in sorted(ov.items()) if k != "timeout"}, default=str)
        return CachedChat(
            inner=self.inner.chat_model(**ov), key_base=base, store=self.cache, spend=self.spend
        )


def pick(qs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ans = [q for q in qs if q["docs"]]
    una = [q for q in qs if not q["docs"]]
    step = len(ans) / N_ANSWERABLE
    return [ans[int(i * step)] for i in range(N_ANSWERABLE)] + una[:N_UNANSWERABLE]


def cited_hit(q: dict[str, Any], res: RagResult, by_chunk: dict[str, str]) -> bool:
    return any(
        judge(
            q,
            c.document_title,
            (c.heading_path or "") + "\n" + by_chunk.get(str(c.chunk_id), c.snippet),
        )
        for c in res.citations
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="off,on")
    ap.add_argument("--budget-yuan", type=float, default=1.0)
    ap.add_argument("--scope", choices=sorted(SCOPES), default="full")
    args = ap.parse_args()
    s = get_settings()
    engine = create_async_engine(
        s.db_url.get_secret_value() if hasattr(s.db_url, "get_secret_value") else s.db_url
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(
        client, prefix="aics_eval_", embed_model=s.embed_model, dim=s.embed_dim
    )
    embedder = CachedEmbedder(build_embedder(s))
    reranker = CachedReranker(build_reranker(s))
    llm = CachedLLM(build_llm(s))
    app_id = await build(sessions, store, embedder, s)
    chunks = PgChunkSearch(sessions)
    ports = RagPorts(
        chunk_search=chunks,
        vector=EmbeddingVectorSearch(embedder, store),
        reranker=reranker,
        llm=llm,
    )
    questions = pick(json.loads((M1 / "questions.json").read_text()))
    profile = AppProfile(name="星河优选", description=SCOPES[args.scope], fallback_text=FALLBACK)
    report: dict[str, Any] = {"n": len(questions), "scope": args.scope, "variants": {}}
    for variant in args.variants.split(","):
        # off / on: generate thinking; norerank: rerank disabled → every question is judged
        # by the LLM judge (exercises the judge prompt live).
        cfg = AppRagConfig(llm_thinking=variant == "on", enable_rerank=variant != "norerank")
        rows = []
        for q in questions:
            if llm.spend.yuan > args.budget_yuan:
                raise SystemExit(f"LLM spend {llm.spend.yuan:.3f} > budget, stopping")
            t0 = time.perf_counter()
            res, _ = await run_rag_collect(
                RagInput(app_id=app_id, question=q["question"], profile=profile, config=cfg),
                ports,
            )
            ms = int((time.perf_counter() - t0) * 1000)
            by_chunk: dict[str, str] = {}
            ids = [c.chunk_id for c in res.citations]
            if ids:
                rows_ = await chunks.fetch_chunks(app_id, ids)
                by_chunk = {str(k): v.text for k, v in rows_.items()}
            answerable = bool(q["docs"])
            if answerable:
                ok = res.answer_type == "answer" and bool(res.citations)
                grounded = ok and cited_hit(q, res, by_chunk)
            else:
                ok = res.answer_type in ("refused", "handoff")
                grounded = ok
            rows.append(
                {
                    "id": q["id"],
                    "answerable": answerable,
                    "ok": ok,
                    "grounded": grounded,
                    "answer_type": res.answer_type,
                    "decision_reason": res.decision_reason,
                    "intent": res.intent.model_dump(),
                    "citations": len(res.citations),
                    "top_rerank": res.trace.get("judge", {}).get("top_score"),
                    "degraded": res.degraded,
                    "usage": res.usage.model_dump(),
                    "wall_ms": ms,
                    "answer": res.answer[:160],
                }
            )
            print(
                q["id"],
                res.answer_type,
                res.decision_reason,
                len(res.citations),
                "ok" if ok else "FAIL",
            )
        a = [r for r in rows if r["answerable"]]
        u = [r for r in rows if not r["answerable"]]
        report["variants"][variant] = {
            "answerable_ok": f"{sum(r['ok'] for r in a)}/{len(a)}",
            "answerable_grounded": f"{sum(r['grounded'] for r in a)}/{len(a)}",
            "unanswerable_refused": f"{sum(r['ok'] for r in u)}/{len(u)}",
            "avg_completion_tokens": round(
                sum(r["usage"]["completion_tokens"] for r in rows) / len(rows), 1
            ),
            "avg_reasoning_tokens": round(
                sum(r["usage"]["reasoning_tokens"] for r in rows) / len(rows), 1
            ),
            "rows": rows,
        }
    gen_lat = [v for v in llm.cache.data.values() if v.get("first_token_ms") is not None]
    report["live_generate_first_token_ms"] = sorted(v["first_token_ms"] for v in gen_lat)
    report["cost_yuan_this_run"] = {
        "llm": round(llm.spend.yuan, 4),
        "llm_calls": llm.spend.calls,
        "llm_tokens": [llm.spend.prompt, llm.spend.completion, llm.spend.reasoning],
        "embed": round(embedder.tokens * EMBED_PRICE / 1e6, 4),
        "rerank": round(reranker.tokens * RERANK_PRICE / 1e6, 4),
    }
    (RES / f"smoke_m23_{args.scope}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1)
    )
    print(json.dumps({k: v for k, v in report.items() if k != "variants"}, ensure_ascii=False))
    for k, v in report["variants"].items():
        print(k, {x: y for x, y in v.items() if x != "rows"})
    await client.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
