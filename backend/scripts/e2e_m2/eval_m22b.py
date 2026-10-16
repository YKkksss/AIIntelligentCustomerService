"""M2.2b retrieval evaluation: fusion × supplementary questions × rerank.

Same corpus / questions / hit rule as eval_m22 (M1 report §4 / §9). Supplementary
questions are generated per chunk with the chat LLM (DeepSeek), standing in for
operator-written questions (设计讨论 0003); the generator never sees the eval questions.
They are attached through the production `KnowledgeService.add_chunk_questions`
(source="llm") and searched by the production pipeline.

Grid: questions {off, on} × fusion {rrf, weighted w∈{0.5,0.6,0.7,0.8,0.9}} × rerank
{off, on}; plus vector-only / fulltext-only references and, for the 11 unanswerable
questions, the share whose top rerank score ≥ rerank_threshold (false-answer risk).

Caches (results/*_cache.json, not committed): embeddings, (query, doc) rerank scores and
generated questions, so re-runs cost ~0. Usage (from backend/, env loaded):
  uv run python -m scripts.e2e_m2.eval_m22b [--per-chunk 3]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import Chunk, Document
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.modules.knowledge.service import MAX_QUESTIONS_PER_CHUNK, KnowledgeService
from aics.providers import build_embedder, build_llm, build_reranker
from aics.rag.config import AppRagConfig
from aics.rag.retrieval import EmbeddingVectorSearch, run_retrieval
from aics.storage.local import LocalStorage
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
    first_hit,
    metrics,
)

LLM_PRICE_IN, LLM_PRICE_OUT = 2.0, 8.0  # yuan / 1M tokens (deepseek-flash, AICS_MODELS)
WEIGHTS = (0.5, 0.6, 0.7, 0.8, 0.9)
PROMPT = """你是客服知识库运营。下面是一段知识库原文。请写出 {n} 个真实用户可能会问、且能由这段原文直接回答的问题：
- 口语化，像用户在聊天框里打字；措辞尽量与原文不同（用同义词、俗称、缩写），但意思准确；
- 每个问题不超过 40 字；只输出问题，每行一个，不要编号、不要解释。

标题路径：{heading}
原文：
{text}"""


class QuestionGen:
    def __init__(self, llm: Any, per_chunk: int) -> None:
        self.model = llm.chat_model(temperature=0.3, max_tokens=400)
        self.name, self.n = llm.default_model, per_chunk
        self.cache = _JsonCache("question_cache.json")
        self.tokens_in = self.tokens_out = 0

    async def __call__(self, heading: str | None, text: str) -> list[str]:
        key = hashlib.sha256(json.dumps([self.name, self.n, heading, text]).encode()).hexdigest()
        if key not in self.cache.data:
            msg = await self.model.ainvoke(
                PROMPT.format(n=self.n, heading=heading or "（无）", text=text[:2000])
            )
            u = msg.usage_metadata or {}
            self.tokens_in += u.get("input_tokens", 0)
            self.tokens_out += u.get("output_tokens", 0)
            lines = [ln.strip(" -•\t").strip() for ln in str(msg.content).splitlines()]
            self.cache.data[key] = [ln for ln in lines if 0 < len(ln) <= 200][: self.n]
            self.cache.save()
        return self.cache.data[key]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-chunk", type=int, default=3)
    args = ap.parse_args()
    s = get_settings()
    engine = create_async_engine(s.db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(
        client, prefix="aics_eval_", embed_model=s.embed_model, dim=s.embed_dim
    )
    embedder = CachedEmbedder(build_embedder(s))
    reranker = CachedReranker(build_reranker(s))
    gen = QuestionGen(build_llm(s), min(args.per_chunk, MAX_QUESTIONS_PER_CHUNK))
    t0 = time.monotonic()
    app_id = await build(sessions, store, embedder, s)

    # ---- generate + attach supplementary questions (production path)
    async with sessions() as db:
        chunks = (
            (
                await db.execute(
                    select(Chunk)
                    .join(Document, Document.id == Chunk.document_id)
                    .where(Chunk.app_id == app_id, Chunk.index_version == Document.index_version)
                    .order_by(Chunk.document_id, Chunk.ordinal)
                )
            )
            .scalars()
            .all()
        )
    sem = asyncio.Semaphore(8)

    async def gen_one(c: Chunk) -> tuple[UUID, list[str]]:
        async with sem:
            return c.id, await gen(c.heading_path, c.text)

    generated = await asyncio.gather(*(gen_one(c) for c in chunks))
    n_q = 0
    for cid, qs_ in generated:
        if not qs_:
            continue
        async with sessions() as db:
            ks = KnowledgeService(
                db, storage=LocalStorage(RES / "storage"), store=store, embedder=embedder
            )
            n_q += len(await ks.add_chunk_questions(app_id, cid, qs_, source="llm"))
    build_s = round(time.monotonic() - t0, 1)

    chunk_search = PgChunkSearch(sessions)
    vec = {
        "q_off": EmbeddingVectorSearch(embedder, store, include_questions=False),
        "q_on": EmbeddingVectorSearch(embedder, store, include_questions=True),
    }
    qs = json.loads((M1 / "questions.json").read_text())
    ans = [q for q in qs if q["docs"]]
    unans = [q for q in qs if not q["docs"]]
    fusions: list[tuple[str, dict[str, Any]]] = [("rrf", {"fusion": "rrf"})] + [
        (f"w{w}", {"fusion": "weighted", "vector_weight": w}) for w in WEIGHTS
    ]

    def units(cands: list[Any]) -> list[tuple[str, str]]:
        return [(c.chunk.document_title, (c.heading_path or "") + "\n" + c.text) for c in cands]

    ranks: dict[str, list[int | None]] = {}
    unans_hits: dict[str, list[bool]] = {}
    examples: dict[str, dict[str, Any]] = {}
    thr = AppRagConfig().rerank_threshold
    for qk, vs in vec.items():
        for mode in ("vector", "fulltext"):
            if mode == "fulltext" and qk == "q_on":
                continue  # questions only affect the vector route
            for q in ans:
                res = await run_retrieval(
                    app_id,
                    q["question"],
                    AppRagConfig(retrieval_mode=mode, enable_rerank=False),
                    vector=vs,
                    chunk_search=chunk_search,
                    reranker=None,
                )
                key = f"{mode}|{qk}" if mode == "vector" else "fulltext"
                ranks.setdefault(key, []).append(first_hit(q, units(res.fused)))
        for fname, fcfg in fusions:
            for rr in (False, True):
                key = f"hybrid|{fname}|{qk}|{'rerank' if rr else 'norerank'}"
                cfg = AppRagConfig(enable_rerank=rr, **fcfg)
                for q in ans:
                    res = await run_retrieval(
                        app_id,
                        q["question"],
                        cfg,
                        vector=vs,
                        chunk_search=chunk_search,
                        reranker=reranker if rr else None,
                    )
                    if rr and not res.rerank_applied:
                        raise RuntimeError(f"rerank degraded: {res.degraded}")
                    out = res.reranked if rr else res.fused
                    r = first_hit(q, units(out))
                    ranks.setdefault(key, []).append(r)
                    if q["id"] in ("q06", "q12", "q29", "q38") and fname == "rrf" and not rr:
                        examples.setdefault(q["id"], {"question": q["question"]})[qk] = r
                if rr:
                    for q in unans:
                        res = await run_retrieval(
                            app_id,
                            q["question"],
                            cfg,
                            vector=vs,
                            chunk_search=chunk_search,
                            reranker=reranker,
                        )
                        top = res.reranked[0].rerank_score if res.reranked else 0.0
                        unans_hits.setdefault(key, []).append((top or 0.0) >= thr)
    report = {
        "n_answerable": len(ans),
        "n_unanswerable": len(unans),
        "chunks": len(chunks),
        "questions_attached": n_q,
        "build_seconds": build_s,
        "metrics": {k: metrics(v) for k, v in ranks.items()},
        "unanswerable_top_rerank_ge_threshold": {
            k: round(sum(v) / len(v), 3) for k, v in unans_hits.items()
        },
        "examples_rank_rrf_norerank": examples,
        "tokens": {
            "embed": embedder.tokens,
            "rerank": reranker.tokens,
            "llm_in": gen.tokens_in,
            "llm_out": gen.tokens_out,
        },
        "cost_yuan_this_run": round(
            embedder.tokens * EMBED_PRICE / 1e6
            + reranker.tokens * RERANK_PRICE / 1e6
            + gen.tokens_in * LLM_PRICE_IN / 1e6
            + gen.tokens_out * LLM_PRICE_OUT / 1e6,
            4,
        ),
    }
    (RES / "retrieval_eval_m22b.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "metrics"}, ensure_ascii=False, indent=1
        )
    )
    for k, v in report["metrics"].items():
        print(f"{k:42s} {v}")
    await client.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
