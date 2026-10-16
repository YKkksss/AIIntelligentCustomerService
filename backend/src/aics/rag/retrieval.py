"""retrieve → fuse → rerank (RAG 流程设计 §4, §4.1).

Pure orchestration over ports: vector search (embedder + Qdrant), keyword search
(`ChunkSearch`), and a `RerankProvider`. The LangGraph nodes (M2.3) call `run_retrieval`
and copy the result into RagState; nothing here touches the database directly.

Rules implemented here:
- the two routes run concurrently; one failing → the other continues and
  `vector_failed` / `keyword_failed` is recorded; both failing → RetrievalUnavailableError;
- vector hits are re-checked against PG (`ChunkSearch.fetch_chunks`) so stale index versions,
  disabled / flagged chunks and deleting documents never come back from Qdrant leftovers;
- fusion is RRF `Σ 1/(k + rank)` with k = 60, dedup by chunk, then (parent-child mode)
  grouped by parent with the best child's score, truncated to `fuse_top_k` units;
- rerank failure → keep RRF order for `rerank_top_n`, record `rerank_failed`,
  `rerank_score=None` (scores are not comparable with thresholds, so judge uses the LLM).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import structlog

from aics.core.errors import AppError
from aics.providers.base import EmbeddingProvider, RerankProvider
from aics.rag.config import AppRagConfig
from aics.rag.ports import ChunkSearch, ChunkText, ParentText
from aics.vectorstore.qdrant import QdrantVectorStore

RRF_K = 60

log = structlog.get_logger()


class RetrievalUnavailableError(AppError):
    code, status, message = "SERVICE_UNAVAILABLE", 503, "检索服务暂不可用"


@dataclass(frozen=True, slots=True)
class RouteHit:
    chunk_id: UUID
    score: float
    rank: int  # 1-based


class VectorSearch(Protocol):
    async def search(self, app_id: UUID, question: str, k: int) -> list[tuple[UUID, float]]: ...


class EmbeddingVectorSearch:
    """Embed the question and query the app's chunk points in Qdrant (enabled only)."""

    def __init__(self, embedder: EmbeddingProvider, store: QdrantVectorStore) -> None:
        self.embedder, self.store = embedder, store

    async def search(self, app_id: UUID, question: str, k: int) -> list[tuple[UUID, float]]:
        vec = await self.embedder.embed_query(question)
        hits = await self.store.search_chunks(app_id, vec, k)
        return [(h.chunk_id, h.score) for h in hits]


@dataclass
class Candidate:
    """One fused unit: a chunk, or a parent block in parent-child mode."""

    unit_id: UUID
    chunk: ChunkText  # best child chunk (the one that is cited)
    parent: ParentText | None
    rrf_score: float
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rerank_score: float | None = None

    @property
    def heading_path(self) -> str | None:
        return self.parent.heading_path if self.parent else self.chunk.heading_path

    @property
    def text(self) -> str:
        return self.parent.text if self.parent else self.chunk.text

    def rerank_text(self) -> str:
        """`heading_path + "\\n" + text` (RAG 流程设计 §4.1 step 3)."""
        return f"{self.heading_path}\n{self.text}" if self.heading_path else self.text

    def trace(self) -> dict[str, Any]:
        return {
            "unit_id": str(self.unit_id),
            "chunk_id": str(self.chunk.chunk_id),
            "parent_id": str(self.parent.parent_id) if self.parent else None,
            "document_id": str(self.chunk.document_id),
            "rrf": round(self.rrf_score, 6),
            "vector_rank": self.vector_rank,
            "keyword_rank": self.keyword_rank,
            "rerank": None if self.rerank_score is None else round(self.rerank_score, 4),
        }


@dataclass
class RetrievalResult:
    vector_hits: list[RouteHit] = field(default_factory=list)
    keyword_hits: list[RouteHit] = field(default_factory=list)
    fused: list[Candidate] = field(default_factory=list)
    reranked: list[Candidate] = field(default_factory=list)
    rerank_applied: bool = False  # True only when real rerank scores are present
    degraded: list[str] = field(default_factory=list)
    timings: dict[str, int] = field(default_factory=dict)

    def trace(self) -> dict[str, Any]:
        return {
            "vector": [(str(h.chunk_id), round(h.score, 4)) for h in self.vector_hits],
            "keyword": [(str(h.chunk_id), round(h.score, 4)) for h in self.keyword_hits],
            "fused": [c.trace() for c in self.fused],
            "reranked": [c.trace() for c in self.reranked],
            "rerank_applied": self.rerank_applied,
            "degraded": list(self.degraded),
            "timings": dict(self.timings),
        }


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _ranked(pairs: Sequence[tuple[UUID, float]]) -> list[RouteHit]:
    seen: set[UUID] = set()
    out: list[RouteHit] = []
    for cid, score in pairs:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(RouteHit(cid, float(score), len(out) + 1))
    return out


async def retrieve(
    app_id: UUID,
    question: str,
    cfg: AppRagConfig,
    *,
    vector: VectorSearch,
    chunk_search: ChunkSearch,
) -> tuple[list[RouteHit], list[RouteHit], list[str]]:
    """Run both routes concurrently; returns (vector_hits, keyword_hits, degraded)."""

    async def kw() -> list[tuple[UUID, float]]:
        hits = await chunk_search.keyword_search(app_id, question, cfg.keyword_top_k)
        return [(h.chunk_id, h.score) for h in hits]

    v_res, k_res = await asyncio.gather(
        vector.search(app_id, question, cfg.vector_top_k), kw(), return_exceptions=True
    )
    degraded: list[str] = []
    if isinstance(v_res, BaseException):
        log.warning("rag.vector_failed", app_id=str(app_id), error_type=type(v_res).__name__)
        degraded.append("vector_failed")
        v_res = []
    if isinstance(k_res, BaseException):
        log.warning("rag.keyword_failed", app_id=str(app_id), error_type=type(k_res).__name__)
        degraded.append("keyword_failed")
        k_res = []
    if len(degraded) == 2:
        raise RetrievalUnavailableError(details={"degraded": degraded})
    return _ranked(v_res), _ranked(k_res), degraded


def rrf_scores(*ranked: Sequence[UUID], k: int = RRF_K) -> dict[UUID, float]:
    """Reciprocal Rank Fusion; each list is in rank order, duplicates inside a list ignored."""
    scores: dict[UUID, float] = {}
    for lst in ranked:
        seen: set[UUID] = set()
        rank = 0
        for cid in lst:
            if cid in seen:
                continue
            seen.add(cid)
            rank += 1
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


async def fuse(
    app_id: UUID,
    vector_hits: list[RouteHit],
    keyword_hits: list[RouteHit],
    cfg: AppRagConfig,
    *,
    chunk_search: ChunkSearch,
) -> list[Candidate]:
    ids = list(
        dict.fromkeys([h.chunk_id for h in vector_hits] + [h.chunk_id for h in keyword_hits])
    )
    rows = await chunk_search.fetch_chunks(app_id, ids)
    # Only currently retrievable chunks take part in ranking (ranks are recomputed after
    # filtering so a stale Qdrant point does not push others down).
    v_ids = [h.chunk_id for h in vector_hits if h.chunk_id in rows]
    k_ids = [h.chunk_id for h in keyword_hits if h.chunk_id in rows]
    scores = rrf_scores(v_ids, k_ids)
    v_rank = {cid: i + 1 for i, cid in enumerate(v_ids)}
    k_rank = {cid: i + 1 for i, cid in enumerate(k_ids)}
    order = sorted(scores, key=lambda c: (-scores[c], v_rank.get(c, 10**6), str(c)))

    parent_ids = [rows[c].parent_id for c in order if rows[c].parent_id is not None]
    parents = await chunk_search.fetch_parents(app_id, parent_ids) if parent_ids else {}
    out: list[Candidate] = []
    by_unit: dict[UUID, Candidate] = {}
    for cid in order:
        ch = rows[cid]
        parent = parents.get(ch.parent_id) if ch.parent_id else None
        unit = parent.parent_id if parent else cid
        if unit in by_unit:  # a better-ranked child of the same parent already holds the slot
            continue
        cand = Candidate(
            unit_id=unit,
            chunk=ch,
            parent=parent,
            rrf_score=scores[cid],
            vector_rank=v_rank.get(cid),
            keyword_rank=k_rank.get(cid),
        )
        by_unit[unit] = cand
        out.append(cand)
        if len(out) >= cfg.fuse_top_k:
            break
    return out


async def rerank(
    question: str,
    candidates: list[Candidate],
    cfg: AppRagConfig,
    *,
    reranker: RerankProvider | None,
) -> tuple[list[Candidate], bool, list[str]]:
    """Returns (top candidates, rerank_applied, degraded)."""
    top_n = cfg.rerank_top_n
    if not candidates:
        return [], False, []
    noop = reranker is None or getattr(reranker, "is_noop", False)
    if not cfg.enable_rerank or noop:
        return candidates[:top_n], False, []
    try:
        scored = await reranker.rerank(  # type: ignore[union-attr]
            question, [c.rerank_text() for c in candidates], top_n
        )
    except Exception as e:  # any failure (timeout, breaker, bad payload) → RRF order
        log.warning("rag.rerank_failed", error_type=type(e).__name__)
        return candidates[:top_n], False, ["rerank_failed"]
    out = []
    for idx, score in scored:
        c = candidates[idx]
        c.rerank_score = score
        out.append(c)
    return out, True, []


async def run_retrieval(
    app_id: UUID,
    question: str,
    cfg: AppRagConfig,
    *,
    vector: VectorSearch,
    chunk_search: ChunkSearch,
    reranker: RerankProvider | None,
) -> RetrievalResult:
    if not question or not question.strip():
        raise ValueError("question must not be empty")
    res = RetrievalResult()
    t0 = time.perf_counter()
    res.vector_hits, res.keyword_hits, deg = await retrieve(
        app_id, question, cfg, vector=vector, chunk_search=chunk_search
    )
    res.degraded += deg
    res.timings["retrieve"] = _ms(t0)
    t1 = time.perf_counter()
    res.fused = await fuse(
        app_id, res.vector_hits, res.keyword_hits, cfg, chunk_search=chunk_search
    )
    res.timings["fuse"] = _ms(t1)
    t2 = time.perf_counter()
    res.reranked, res.rerank_applied, deg = await rerank(
        question, res.fused, cfg, reranker=reranker
    )
    res.degraded += deg
    res.timings["rerank"] = _ms(t2)
    return res
