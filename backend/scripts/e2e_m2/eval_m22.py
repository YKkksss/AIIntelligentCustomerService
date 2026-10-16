"""M2.2 retrieval evaluation through the production pipeline (aics.rag.retrieval).

Builds the M1 main corpus (scripts/e2e_m1/out, run_e2e.MAIN_DOCS, general preset) into the
DB given by AICS_DB_URL with the real embedder, stores vectors in qdrant-client *local*
mode (no server needed), then compares on scripts/e2e_m1/questions.json:

- fulltext (old stopwords) vs fulltext (QUERY_STOPWORDS, M2.2 default)
- vector, hybrid RRF (k=60), hybrid RRF + rerank (production `run_retrieval`)
- offline weighted fusion (min-max normalised, w_vec ∈ {0.5..0.9}) as M2.2b input only

Cost control: document/query embeddings and (query, doc) rerank scores are cached under
results/*_cache.json, so re-runs only pay for new text. Usage (from backend/):
  set env (AICS_DB_URL, AICS_EMBED_API_KEY, AICS_RERANK_API_KEY ...), then
  uv run python -m scripts.e2e_m2.eval_m22 [--rebuild]
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.core.textsearch import STOPWORDS, build_tsquery
from aics.core.tokens import count_tokens
from aics.db.models import App, Chunk, ChunkParent, Document, Job, User
from aics.ingest.service import IngestService
from aics.modules.apps.service import AppService
from aics.modules.knowledge import chunk_search as cs_mod
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.modules.knowledge.service import KnowledgeService
from aics.providers import build_embedder, build_reranker
from aics.rag.config import AppRagConfig
from aics.rag.retrieval import EmbeddingVectorSearch, run_retrieval
from aics.storage.local import LocalStorage
from aics.vectorstore import QdrantVectorStore
from scripts.e2e_m1.run_e2e import MAIN_DOCS

HERE = Path(__file__).parent
M1 = HERE.parent / "e2e_m1"
RES = HERE / "results"
APP_NAME = "e2e-m22-main"
EMBED_PRICE, RERANK_PRICE = 0.125, 0.5  # yuan / 1M tokens (AICS_MODELS registry)


class _JsonCache:
    def __init__(self, name: str) -> None:
        self.path = RES / name
        self.data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data))


class CachedEmbedder:
    def __init__(self, inner: Any) -> None:
        self.inner, self.model, self.dim = inner, inner.model, inner.dim
        self.cache = _JsonCache("embed_cache.json")
        self.tokens = 0

    def _k(self, t: str) -> str:
        return hashlib.sha256(json.dumps([self.model, self.dim, t]).encode()).hexdigest()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        miss = [t for t in dict.fromkeys(texts) if self._k(t) not in self.cache.data]
        if miss:
            for t, v in zip(miss, await self.inner.embed_documents(miss), strict=True):
                self.cache.data[self._k(t)] = v
            self.tokens += sum(count_tokens(t) for t in miss)
            self.cache.save()
        return [self.cache.data[self._k(t)] for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        k = "q:" + self._k(text)
        if k not in self.cache.data:
            self.cache.data[k] = await self.inner.embed_query(text)
            self.tokens += count_tokens(text)
            self.cache.save()
        return self.cache.data[k]


class CachedReranker:
    """Cross-encoder scores are per (query, doc) pair, so they cache independently."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.model = inner, inner.model
        self.cache = _JsonCache("rerank_pair_cache.json")
        self.tokens = 0

    def _k(self, q: str, d: str) -> str:
        return hashlib.sha256(json.dumps([self.model, q, d]).encode()).hexdigest()

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        miss = [d for d in dict.fromkeys(docs) if self._k(query, d) not in self.cache.data]
        if miss:
            for i, s in await self.inner.rerank(query, miss, len(miss)):
                self.cache.data[self._k(query, miss[i])] = s
            self.tokens += sum(count_tokens(d) for d in miss) + count_tokens(query) * len(miss)
            self.cache.save()
        sc = [(i, self.cache.data[self._k(query, d)]) for i, d in enumerate(docs)]
        return sorted(sc, key=lambda x: -x[1])[:top_n]


def judge(q: dict[str, Any], title: str, hay: str) -> bool:
    return any(title.startswith(d) for d in q["docs"]) and any(e in hay for e in q["evidence"])


def first_hit(q: dict[str, Any], units: list[tuple[str, str]]) -> int | None:
    return next((i for i, (t, h) in enumerate(units, 1) if judge(q, t, h)), None)


def metrics(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks)
    return {
        "hit@1": round(sum(1 for r in ranks if r == 1) / n, 3),
        "hit@3": round(sum(1 for r in ranks if r and r <= 3) / n, 3),
        "hit@5": round(sum(1 for r in ranks if r and r <= 5) / n, 3),
        "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
    }


async def build(sessions: Any, store: Any, embedder: Any, s: Any) -> UUID:
    async with sessions() as db:
        old = (await db.execute(select(App.id).where(App.name == APP_NAME))).scalars().all()
        for a in old:
            for m in (Chunk, ChunkParent, Job, Document, App):
                col = m.id if m is App else m.app_id
                await db.execute(delete(m).where(col == a))
        u = (await db.execute(select(User).where(User.email == "e2e-m22@local"))).scalar()
        if u is None:
            u = User(email="e2e-m22@local", display_name="e2e", password_hash="x")  # noqa: S106 - eval-only user, never logs in
            db.add(u)
            await db.flush()
        app = await AppService(db).create_app(
            name=APP_NAME, created_by=u.id, chunk_config={"preset": "general"}
        )
        await db.commit()
    storage = LocalStorage(RES / "storage")
    queue: list[tuple[str, list[Any]]] = []
    async with sessions() as db:
        ks = KnowledgeService(
            db, storage=storage, store=store, enqueue=lambda t, a: queue.append((t, a))
        )
        for n in MAIN_DOCS:
            await ks.upload_file(app.id, (M1 / "out" / n).read_bytes(), n)
        await db.commit()
    svc = IngestService(
        sessions,
        storage=storage,
        store=store,
        embedder=embedder,
        settings=s,
        enqueue_cleanup=lambda *a: None,
    )
    for _, args in queue:
        st = await svc.run_build(UUID(args[0]), UUID(args[1]))
        print("build", args[0], st)
    return app.id


def weighted(vh: list[Any], kh: list[Any], w: float) -> list[UUID]:
    def norm(hits: list[Any]) -> dict[UUID, float]:
        if not hits:
            return {}
        lo, hi = min(h.score for h in hits), max(h.score for h in hits)
        return {h.chunk_id: (h.score - lo) / (hi - lo) if hi > lo else 1.0 for h in hits}

    v, k = norm(vh), norm(kh)
    ids = set(v) | set(k)
    return sorted(ids, key=lambda c: -(w * v.get(c, 0.0) + (1 - w) * k.get(c, 0.0)))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-rerank", action="store_true")
    args = ap.parse_args()
    s = get_settings()
    engine = create_async_engine(
        s.db_url.get_secret_value() if hasattr(s.db_url, "get_secret_value") else s.db_url
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncQdrantClient(location=":memory:")  # rebuilt every run; embeddings are cached
    store = QdrantVectorStore(
        client, prefix="aics_eval_", embed_model=s.embed_model, dim=s.embed_dim
    )
    embedder = CachedEmbedder(build_embedder(s))
    reranker = None if args.no_rerank else CachedReranker(build_reranker(s))
    t0 = time.monotonic()
    app_id = await build(sessions, store, embedder, s)
    build_s = round(time.monotonic() - t0, 1)
    chunks = PgChunkSearch(sessions)
    vector = EmbeddingVectorSearch(embedder, store)
    cfg = AppRagConfig()
    qs = json.loads((M1 / "questions.json").read_text())
    ans = [q for q in qs if q["docs"]]

    async def units(ids: list[UUID]) -> list[tuple[str, str]]:
        rows = await chunks.fetch_chunks(app_id, ids)
        return [
            (rows[i].document_title, (rows[i].heading_path or "") + "\n" + rows[i].text)
            for i in ids
            if i in rows
        ]

    ranks: dict[str, list[int | None]] = {}
    old_tsq = functools.partial(build_tsquery, stopwords=STOPWORDS)
    for q in ans:
        cs_mod.build_tsquery = old_tsq  # baseline: M1 stopword list
        kh_old = await chunks.keyword_search(app_id, q["question"], cfg.keyword_top_k)
        cs_mod.build_tsquery = build_tsquery
        res = await run_retrieval(
            app_id, q["question"], cfg, vector=vector, chunk_search=chunks, reranker=reranker
        )

        def cand_units(cands: list[Any]) -> list[tuple[str, str]]:
            return [
                (
                    c.chunk.document_title,
                    (c.parent.heading_path or "") + "\n" + c.parent.text
                    if c.parent
                    else (c.chunk.heading_path or "") + "\n" + c.chunk.text,
                )
                for c in cands
            ]

        rows = {
            "fulltext_old": await units([h.chunk_id for h in kh_old]),
            "fulltext_new": await units([h.chunk_id for h in res.keyword_hits]),
            "vector": await units([h.chunk_id for h in res.vector_hits]),
            "hybrid_rrf": cand_units(res.fused),
        }
        if reranker is not None:
            if not res.rerank_applied:
                raise RuntimeError(f"rerank degraded: {res.degraded}")
            rows["hybrid_rrf+rerank"] = cand_units(res.reranked)
        for w in (0.5, 0.6, 0.7, 0.8, 0.9):
            rows[f"weighted_v{w}"] = await units(
                weighted(res.vector_hits, res.keyword_hits, w)[: cfg.fuse_top_k]
            )
        for k, u in rows.items():
            ranks.setdefault(k, []).append(first_hit(q, u))
    report = {
        "n_answerable": len(ans),
        "build_seconds": build_s,
        "metrics": {k: metrics(v) for k, v in ranks.items()},
        "fulltext_misses_new": [
            q["id"] for q, r in zip(ans, ranks["fulltext_new"], strict=True) if not r
        ],
        "cost_yuan": round(
            embedder.tokens * EMBED_PRICE / 1e6
            + (reranker.tokens if reranker else 0) * RERANK_PRICE / 1e6,
            4,
        ),
        "tokens": {"embed": embedder.tokens, "rerank": reranker.tokens if reranker else 0},
    }
    (RES / "retrieval_eval_m22.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    await client.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
