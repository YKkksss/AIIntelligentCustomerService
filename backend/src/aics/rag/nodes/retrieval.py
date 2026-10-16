"""retrieve → fuse → rerank nodes; thin wrappers over `aics.rag.retrieval` (M2.2 / M2.2b)."""

from __future__ import annotations

import time
from typing import Any

from langgraph.runtime import Runtime

from aics.rag import retrieval as r
from aics.rag.state import RagDeps, RagState, merged


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _query(state: RagState) -> str:
    return state.get("rewritten_query") or state["input"].question


async def retrieve(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    cfg = inp.config
    ports = runtime.context.ports
    vector = ports.vector
    if vector is None:
        if cfg.retrieval_mode != "fulltext":
            raise ValueError("RagPorts.vector is required unless retrieval_mode='fulltext'")
        vector = _NoVector()
    tags = list(cfg.default_tags if inp.tags is None else inp.tags)
    t0 = time.perf_counter()
    v, k, deg = await r.retrieve(
        inp.app_id,
        _query(state),
        cfg,
        vector=vector,
        chunk_search=ports.chunk_search,
        tags=tags or None,
    )
    return merged(
        state,
        vector_hits=v,
        keyword_hits=k,
        tags=tags,
        degraded=deg,
        timings={"retrieve": _ms(t0)},
    )


async def fuse(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    t0 = time.perf_counter()
    fused = await r.fuse(
        inp.app_id,
        state.get("vector_hits", []),
        state.get("keyword_hits", []),
        inp.config,
        chunk_search=runtime.context.ports.chunk_search,
        tags=state.get("tags") or None,
    )
    return merged(state, fused=fused, timings={"fuse": _ms(t0)})


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
