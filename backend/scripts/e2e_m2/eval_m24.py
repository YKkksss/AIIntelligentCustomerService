"""M2.4 intent evaluation through the production `run_rag` (意图识别 §12).

Cases: scripts/e2e_m2/intent_cases.json (chitchat / out_of_scope / clarify / human / action /
injection / kb_qa, incl. corpus questions prone to out-of-scope misclassification such as q26).
Corpus, caches and the "full" app description are those of smoke_m23 (real DeepSeek + DashScope,
responses cached, so re-runs are free).

Metrics (§12): intent accuracy, chitchat false-refusal rate, false-handoff rate, kb_qa miss
rate, confusion matrix; plus the out-of-scope safeguard sweep — for every case the LLM labelled
out_of_scope, the scope_check top rerank score, and per threshold the kb_qa questions recovered
vs true out-of-scope questions wrongly overridden.
Usage (from backend/, env loaded):
  uv run python -m scripts.e2e_m2.eval_m24 [--budget-yuan 0.8] [--scope full|narrow]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.providers import build_embedder, build_llm, build_reranker
from aics.rag.config import AppRagConfig
from aics.rag.graph import run_rag_collect
from aics.rag.ports import ChatTurn, RagPorts
from aics.rag.retrieval import EmbeddingVectorSearch
from aics.rag.schemas import AppProfile, RagInput
from aics.vectorstore import QdrantVectorStore
from scripts.e2e_m2.eval_m22 import (
    EMBED_PRICE,
    RERANK_PRICE,
    RES,
    CachedEmbedder,
    CachedReranker,
    build,
)
from scripts.e2e_m2.smoke_m23 import FALLBACK, SCOPES, CachedLLM

CASES = Path(__file__).with_name("intent_cases.json")
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


def rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-yuan", type=float, default=0.8)
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
    ports = RagPorts(
        chunk_search=PgChunkSearch(sessions),
        vector=EmbeddingVectorSearch(embedder, store),
        reranker=reranker,
        llm=llm,
    )
    profile = AppProfile(name="星河优选", description=SCOPES[args.scope], fallback_text=FALLBACK)
    cases = json.loads(CASES.read_text())["cases"]
    rows: list[dict[str, Any]] = []
    for c in cases:
        if llm.spend.yuan > args.budget_yuan:
            raise SystemExit(f"LLM spend {llm.spend.yuan:.3f} > budget, stopping")
        hist = [ChatTurn(**t) for t in c.get("history", [])] or None
        res, _ = await run_rag_collect(
            RagInput(
                app_id=app_id,
                question=c["question"],
                profile=profile,
                config=AppRagConfig(),
                inline_history=hist,
            ),
            ports,
        )
        it = res.trace.get("intent", {})
        sc = it.get("scope_check") or {}
        row = {
            "id": c["id"],
            "category": c["category"],
            "expected": c["expected_intent"],
            "intent": res.intent.name,
            "source": res.intent.source,
            "raw_intent": it.get("raw_intent"),
            "fallback_reason": it.get("fallback_reason"),
            "scope_top_score": sc.get("top_score"),
            "answer_type": res.answer_type,
            "decision_reason": res.decision_reason,
            "citations": len(res.citations),
            "answer": res.answer[:120],
        }
        rows.append(row)
        print(
            row["id"],
            row["expected"],
            "→",
            row["intent"],
            row["source"],
            row["answer_type"],
            row["decision_reason"],
            sc.get("top_score"),
        )
    n = len(rows)
    ok = sum(r["intent"] == r["expected"] for r in rows)
    chit = [r for r in rows if r["expected"] == "chitchat"]
    non_handoff = [r for r in rows if r["expected"] not in ("human", "action")]
    kb = [r for r in rows if r["expected"] == "kb_qa"]
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        confusion[r["expected"]][r["intent"]] += 1
    # Safeguard sweep: what each threshold would have done on LLM out_of_scope verdicts.
    oos = [r for r in rows if r["raw_intent"] == "out_of_scope"]
    sweep = {}
    for t in THRESHOLDS:
        over = [r for r in oos if r["scope_top_score"] is not None and r["scope_top_score"] >= t]
        sweep[str(t)] = {
            "kb_qa_recovered": [r["id"] for r in over if r["expected"] == "kb_qa"],
            "wrong_override": [r["id"] for r in over if r["expected"] != "kb_qa"],
        }
    report = {
        "scope": args.scope,
        "n": n,
        "intent_accuracy": rate(ok, n),
        "chitchat_false_refusal": rate(
            sum(r["answer_type"] in ("refused", "handoff") for r in chit), len(chit)
        ),
        "false_handoff": rate(
            sum(r["answer_type"] == "handoff" for r in non_handoff), len(non_handoff)
        ),
        "kb_qa_miss": rate(sum(r["intent"] != "kb_qa" for r in kb), len(kb)),
        "kb_qa_miss_without_safeguard": rate(
            sum(r["intent"] != "kb_qa" or r["fallback_reason"] == "kb_evidence" for r in kb),
            len(kb),
        ),
        "by_source": dict(Counter(r["source"] for r in rows)),
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "out_of_scope_scores": [(r["id"], r["expected"], r["scope_top_score"]) for r in oos],
        "safeguard_sweep": sweep,
        "cost_yuan_this_run": {
            "llm": round(llm.spend.yuan, 4),
            "llm_calls": llm.spend.calls,
            "embed": round(embedder.tokens * EMBED_PRICE / 1e6, 4),
            "rerank": round(reranker.tokens * RERANK_PRICE / 1e6, 4),
        },
        "rows": rows,
    }
    (RES / f"intent_eval_m24_{args.scope}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1)
    )
    print(
        json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=1)
    )
    await client.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
