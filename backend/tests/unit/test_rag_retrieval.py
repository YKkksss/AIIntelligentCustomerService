"""M2.2: retrieve → fuse (RRF, parent grouping) → rerank with fakes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from uuid import UUID

import pytest

from aics.providers import NoopReranker, UpstreamUnavailableError
from aics.rag.config import AppRagConfig
from aics.rag.ports import ChunkText, KeywordHit, ParentText
from aics.rag.retrieval import (
    RRF_K,
    RetrievalUnavailableError,
    rrf_scores,
    run_retrieval,
)

APP = uuid.uuid4()
DOC = uuid.uuid4()


def ids(n: int) -> list[UUID]:
    return [uuid.UUID(int=i + 1) for i in range(n)]


C = ids(10)
P1 = uuid.UUID(int=1000)


def chunk(cid: UUID, parent: UUID | None = None) -> ChunkText:
    return ChunkText(cid, DOC, "Doc", "H", f"text-{cid.int}", None, parent)


class FakeChunks:
    def __init__(
        self,
        kw: list[UUID],
        retrievable: set[UUID],
        *,
        parents: dict[UUID, UUID] | None = None,
        fail: bool = False,
    ) -> None:
        self.kw, self.retrievable, self.fail = kw, retrievable, fail
        self.parents = parents or {}

    async def keyword_search(self, app_id: UUID, question: str, k: int) -> list[KeywordHit]:
        if self.fail:
            raise RuntimeError("pg down")
        return [KeywordHit(c, 1.0 / (i + 1)) for i, c in enumerate(self.kw[:k])]

    async def fetch_chunks(self, app_id: UUID, chunk_ids: Sequence[UUID]) -> dict[UUID, ChunkText]:
        return {c: chunk(c, self.parents.get(c)) for c in chunk_ids if c in self.retrievable}

    async def fetch_parents(
        self, app_id: UUID, parent_ids: Sequence[UUID]
    ) -> dict[UUID, ParentText]:
        return {p: ParentText(p, DOC, "Doc", "PH", f"parent-{p.int}", None) for p in parent_ids}


class FakeVector:
    def __init__(self, hits: list[UUID], fail: bool = False) -> None:
        self.hits, self.fail = hits, fail

    async def search(self, app_id: UUID, question: str, k: int) -> list[tuple[UUID, float]]:
        if self.fail:
            raise UpstreamUnavailableError("embedding down")
        return [(c, 0.9 - i * 0.01) for i, c in enumerate(self.hits[:k])]


class FakeReranker:
    model = "fake"

    def __init__(self, prefer: UUID | None = None, fail: bool = False) -> None:
        self.prefer, self.fail = prefer, fail
        self.docs: list[str] = []

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        if self.fail:
            raise UpstreamUnavailableError("rerank down")
        self.docs = docs
        scored = [
            (i, 0.99 if self.prefer and f"-{self.prefer.int}" in d else 0.1)
            for i, d in enumerate(docs)
        ]
        return sorted(scored, key=lambda x: -x[1])[:top_n]


CFG = AppRagConfig()


def test_rrf_scores() -> None:
    s = rrf_scores([C[0], C[1]], [C[1], C[2], C[1]])
    assert s[C[1]] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert s[C[2]] == pytest.approx(1 / (RRF_K + 2))  # duplicate inside a list ignored
    assert max(s, key=s.get) == C[1]


async def test_hybrid_rerank_happy_path() -> None:
    ch = FakeChunks(kw=[C[2], C[1]], retrievable=set(C))
    rr = FakeReranker(prefer=C[2])
    res = await run_retrieval(
        APP, "q", CFG, vector=FakeVector([C[0], C[1]]), chunk_search=ch, reranker=rr
    )
    # C0 (vector #1) and C2 (keyword #1) tie on RRF; ties break by vector rank
    assert [c.chunk.chunk_id for c in res.fused] == [C[1], C[0], C[2]]
    assert res.fused[0].vector_rank == 2 and res.fused[0].keyword_rank == 2
    assert res.rerank_applied and res.degraded == []
    assert res.reranked[0].chunk.chunk_id == C[2] and res.reranked[0].rerank_score == 0.99
    assert rr.docs[0] == "H\ntext-2"  # heading_path + "\n" + text
    assert set(res.timings) == {"retrieve", "fuse", "rerank"}
    assert res.trace()["reranked"][0]["rerank"] == 0.99


async def test_stale_and_flagged_vector_hits_dropped() -> None:
    ch = FakeChunks(kw=[], retrievable={C[1]})
    res = await run_retrieval(
        APP, "q", CFG, vector=FakeVector([C[0], C[1]]), chunk_search=ch, reranker=None
    )
    assert [c.chunk.chunk_id for c in res.fused] == [C[1]]
    assert res.fused[0].vector_rank == 1  # rank recomputed after filtering


@pytest.mark.parametrize(
    ("vfail", "kfail", "flag"), [(True, False, "vector_failed"), (False, True, "keyword_failed")]
)
async def test_one_route_fails(vfail: bool, kfail: bool, flag: str) -> None:
    ch = FakeChunks(kw=[C[3]], retrievable=set(C), fail=kfail)
    res = await run_retrieval(
        APP, "q", CFG, vector=FakeVector([C[4]], fail=vfail), chunk_search=ch, reranker=None
    )
    assert res.degraded == [flag] and len(res.fused) == 1


async def test_both_routes_fail() -> None:
    with pytest.raises(RetrievalUnavailableError) as ei:
        await run_retrieval(
            APP,
            "q",
            CFG,
            vector=FakeVector([], fail=True),
            chunk_search=FakeChunks([], set(), fail=True),
            reranker=None,
        )
    assert ei.value.status == 503


async def test_rerank_failure_keeps_rrf_order() -> None:
    cfg = AppRagConfig(rerank_top_n=2)
    ch = FakeChunks(kw=[C[2], C[1]], retrievable=set(C))
    res = await run_retrieval(
        APP,
        "q",
        cfg,
        vector=FakeVector([C[0], C[1]]),
        chunk_search=ch,
        reranker=FakeReranker(fail=True),
    )
    assert res.degraded == ["rerank_failed"] and not res.rerank_applied
    assert [c.chunk.chunk_id for c in res.reranked] == [C[1], C[0]]
    assert all(c.rerank_score is None for c in res.reranked)


@pytest.mark.parametrize("reranker", [None, NoopReranker(), FakeReranker(prefer=C[0])])
async def test_rerank_disabled_or_noop_not_degraded(reranker: object) -> None:
    cfg = AppRagConfig(
        enable_rerank=reranker is not None and not isinstance(reranker, FakeReranker)
    )
    ch = FakeChunks(kw=[C[1]], retrievable=set(C))
    res = await run_retrieval(
        APP,
        "q",
        cfg,
        vector=FakeVector([C[0]]),
        chunk_search=ch,
        reranker=reranker,  # type: ignore[arg-type]
    )
    assert res.degraded == [] and not res.rerank_applied
    assert [c.chunk.chunk_id for c in res.reranked] == [c.chunk.chunk_id for c in res.fused][:5]


async def test_parent_child_grouping() -> None:
    parents = {C[0]: P1, C[1]: P1}
    ch = FakeChunks(kw=[C[1], C[2]], retrievable=set(C), parents=parents)
    rr = FakeReranker(prefer=P1)
    res = await run_retrieval(
        APP, "q", CFG, vector=FakeVector([C[0], C[1]]), chunk_search=ch, reranker=rr
    )
    units = [c.unit_id for c in res.fused]
    assert units == [P1, C[2]]  # two children of P1 take one slot
    pc = next(c for c in res.fused if c.unit_id == P1)
    assert pc.chunk.chunk_id == C[1]  # best child (in both routes) is the cited chunk
    assert pc.rerank_text() == "PH\nparent-1000"
    assert res.reranked[0].unit_id == P1


async def test_fuse_top_k_and_empty() -> None:
    cfg = AppRagConfig(fuse_top_k=3)
    ch = FakeChunks(kw=C[5:], retrievable=set(C))
    res = await run_retrieval(
        APP, "q", cfg, vector=FakeVector(C[:5]), chunk_search=ch, reranker=None
    )
    assert len(res.fused) == 3
    empty = await run_retrieval(
        APP,
        "q",
        CFG,
        vector=FakeVector([]),
        chunk_search=FakeChunks([], set()),
        reranker=FakeReranker(),
    )
    assert empty.fused == [] and empty.reranked == [] and not empty.rerank_applied
    with pytest.raises(ValueError):
        await run_retrieval(APP, " ", CFG, vector=FakeVector([]), chunk_search=ch, reranker=None)
