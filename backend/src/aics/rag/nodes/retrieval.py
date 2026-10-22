"""retrieve → fuse → rerank nodes; thin wrappers over `aics.rag.retrieval` (M2.2 / M2.2b).

`retrieve_and_fuse` is also used by condense_classify (parallel mode, 意图识别 §5.4) and by
the out-of-scope safeguard (`scope_check`, 设计讨论 0005).
"""

from __future__ import annotations

import time
from typing import Any

from langgraph.runtime import Runtime

from aics.rag import retrieval as r
from aics.rag.ports import RagPorts
from aics.rag.schemas import RagInput
from aics.rag.state import RagDeps, RagState, merged


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _query(state: RagState) -> str:
    return state.get("rewritten_query") or state["input"].question


async def retrieve_and_fuse(inp: RagInput, ports: RagPorts, query: str) -> dict[str, Any]:
    """retrieve + fuse outside the graph edges; returns raw updates (vector_hits,
    keyword_hits, fused, tags, degraded, timings). Raises RetrievalUnavailableError."""
    upd = await _routes(inp, ports, query)
    f = await _fuse(inp, ports, upd)
    return {**upd, "fused": f["fused"], "timings": {**upd["timings"], **f["timings"]}}


async def _routes(inp: RagInput, ports: RagPorts, query: str) -> dict[str, Any]:
    cfg = inp.config
    vector = ports.vector
    if vector is None:
        if cfg.retrieval_mode != "fulltext":
            raise ValueError("RagPorts.vector is required unless retrieval_mode='fulltext'")
        vector = _NoVector()
    tags = list(cfg.default_tags if inp.tags is None else inp.tags)
    t0 = time.perf_counter()
    v, k, deg = await r.retrieve(
        inp.app_id,
        query,
        cfg,
        vector=vector,
        chunk_search=ports.chunk_search,
        tags=tags or None,
        kb_ids=inp.kb_ids or None,
    )
    return {
        "vector_hits": v,
        "keyword_hits": k,
        "tags": tags,
        "degraded": deg,
        "timings": {"retrieve": _ms(t0)},
    }


async def _fuse(inp: RagInput, ports: RagPorts, upd: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    fused = await r.fuse(
        inp.app_id,
        upd.get("vector_hits", []),
        upd.get("keyword_hits", []),
        inp.config,
        chunk_search=ports.chunk_search,
        tags=upd.get("tags") or None,
        kb_ids=inp.kb_ids or None,
    )
    return {"fused": fused, "timings": {"fuse": _ms(t0)}}


async def retrieve(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    upd = await _routes(state["input"], runtime.context.ports, _query(state))
    return merged(state, **upd)


async def fuse(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    upd = await _fuse(state["input"], runtime.context.ports, dict(state))
    return merged(state, **upd)


async def rerank(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    t0 = time.perf_counter()
    top, applied, deg = await r.rerank(
        _query(state),
        state.get("fused", []),
        state["input"].config,
        reranker=runtime.context.ports.reranker,
    )
    return merged(
        state, reranked=top, rerank_applied=applied, degraded=deg, timings={"rerank": _ms(t0)}
    )


class _NoVector:
    async def search(self, *a: Any, **k: Any) -> list[r.RouteHit]:  # never called (fulltext)
        return []
