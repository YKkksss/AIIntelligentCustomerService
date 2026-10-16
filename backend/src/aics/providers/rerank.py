"""Rerank providers (模型供应商抽象 §1–§3): DashScope text-rerank, llama.cpp /v1/rerank, none.

All implementations return `[(doc_index, score)]` sorted by score descending, at most `top_n`
items, indices referring to the input `docs`. Timeouts, retries and the circuit breaker come
from `providers.resilience`; callers (rag rerank step) treat any exception as `rerank_failed`.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from aics.providers.base import UpstreamError
from aics.providers.resilience import CircuitBreaker, call_with_resilience, status_retryable

RERANK_TIMEOUT_S = 10.0
# DashScope text-rerank accepts at most 500 documents per request; fuse_top_k is ≤ 200.
MAX_DOCS = 500

log = structlog.get_logger()


def _check_args(docs: list[str], top_n: int) -> None:
    if top_n < 1:
        raise ValueError("top_n must be ≥ 1")
    if len(docs) > MAX_DOCS:
        raise ValueError(f"at most {MAX_DOCS} documents per rerank call")


def parse_results(items: list[dict[str, Any]], n_docs: int, top_n: int) -> list[tuple[int, float]]:
    """Validate upstream items `{index, relevance_score}` and sort them by score desc."""
    out: list[tuple[int, float]] = []
    seen: set[int] = set()
    for it in items:
        try:
            idx, score = int(it["index"]), float(it["relevance_score"])
        except (KeyError, TypeError, ValueError) as e:
            raise UpstreamError(f"rerank item malformed: {e}") from e
        if not 0 <= idx < n_docs or idx in seen:
            raise UpstreamError(f"rerank index out of range or duplicated: {idx}")
        seen.add(idx)
        out.append((idx, score))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out[:top_n]


class _HttpReranker:
    provider = "http"

    def __init__(
        self,
        *,
        url: str,
        model: str,
        api_key: str | None,
        timeout_s: float = RERANK_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self._url = url
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self.breaker = breaker or CircuitBreaker(f"rerank:{self.provider}")

    async def aclose(self) -> None:
        await self._client.aclose()

    def _body(self, query: str, docs: list[str], top_n: int) -> dict[str, Any]:
        raise NotImplementedError

    def _items(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def _post(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        resp = await self._client.post(
            self._url, json=self._body(query, docs, top_n), headers=self._headers
        )
        if resp.status_code != 200:
            raise UpstreamError(
                f"rerank upstream returned {resp.status_code}",
                status=resp.status_code,
                retryable=status_retryable(resp.status_code),
            )
        try:
            data = resp.json()
            items = self._items(data)
        except (ValueError, KeyError, TypeError) as e:
            raise UpstreamError(f"rerank response malformed: {type(e).__name__}") from e
        usage = data.get("usage") or {}
        log.debug(
            "provider.reranked", model=self.model, n=len(docs), tokens=usage.get("total_tokens")
        )
        return parse_results(items, len(docs), top_n)

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        _check_args(docs, top_n)
        if not docs:
            return []
        if not query.strip():
            raise ValueError("rerank query must not be empty")
        top_n = min(top_n, len(docs))
        return await call_with_resilience(
            lambda: self._post(query, docs, top_n), breaker=self.breaker, timeout_s=self.timeout_s
        )


class DashScopeReranker(_HttpReranker):
    """`POST …/services/rerank/text-rerank/text-rerank` (qwen3.7-text-rerank)."""

    provider = "dashscope"

    def _body(self, query: str, docs: list[str], top_n: int) -> dict[str, Any]:
        return {
            "model": self.model,
            "input": {"query": query, "documents": docs},
            "parameters": {"top_n": top_n, "return_documents": False},
        }

    def _items(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        return list(data["output"]["results"])


class LlamaCppReranker(_HttpReranker):
    """llama.cpp server `POST /v1/rerank` (local Qwen3-Reranker, Jina/Cohere-style payload)."""

    provider = "llamacpp"

    def _body(self, query: str, docs: list[str], top_n: int) -> dict[str, Any]:
        return {"model": self.model, "query": query, "documents": docs, "top_n": top_n}

    def _items(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        return list(data["results"])


class NoopReranker:
    """`AICS_RERANK_PROVIDER=none`: keeps the input order. Scores are rank-derived placeholders
    and are NOT comparable with rerank thresholds; callers check `is_noop` and skip judging by
    score (RAG 流程设计 §4: rerank disabled → LLM judge)."""

    model = "none"
    is_noop = True

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        _check_args(docs, top_n)
        n = min(top_n, len(docs))
        return [(i, 1.0 - i / max(len(docs), 1)) for i in range(n)]
