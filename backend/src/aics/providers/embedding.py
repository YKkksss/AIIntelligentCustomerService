"""向量化实现：OpenAI 兼容 `/embeddings`（DashScope 兼容模式、llama.cpp 等）。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from aics.providers.base import DimensionMismatchError, UpstreamError
from aics.providers.resilience import CircuitBreaker, call_with_resilience, status_retryable

EMBED_TIMEOUT_S = 20.0
MAX_CONCURRENCY = 4

log = structlog.get_logger()


class OpenAICompatibleEmbedder:
    """批量向量化；并发 ≤ 4；每批独立重试；返回维度必须等于 `dim`。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 10,
        timeout_s: float = EMBED_TIMEOUT_S,
        concurrency: int = MAX_CONCURRENCY,
        send_dimensions: bool = True,
        client: httpx.AsyncClient | None = None,
        breaker: CircuitBreaker | None = None,
        provider: str = "openai_compatible",
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size 必须 ≥ 1")
        self.model, self.dim = model, dim
        self.batch_size, self.timeout_s = batch_size, timeout_s
        self.send_dimensions = send_dimensions
        self.provider = provider
        self._url = base_url.rstrip("/") + "/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._sem = asyncio.Semaphore(concurrency)
        self.breaker = breaker or CircuitBreaker(f"embedding:{provider}")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, texts: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self.model, "input": texts, "encoding_format": "float"}
        if self.send_dimensions:
            body["dimensions"] = self.dim
        resp = await self._client.post(self._url, json=body, headers=self._headers)
        if resp.status_code != 200:
            raise UpstreamError(
                f"embedding 上游返回 {resp.status_code}",
                status=resp.status_code,
                retryable=status_retryable(resp.status_code),
            )
        data = resp.json()
        items = sorted(data.get("data", []), key=lambda d: d["index"])
        if len(items) != len(texts):
            raise UpstreamError(f"embedding 返回 {len(items)} 条，期望 {len(texts)} 条")
        vectors = [list(map(float, it["embedding"])) for it in items]
        for v in vectors:
            if len(v) != self.dim:
                raise DimensionMismatchError(
                    f"embedding 维度 {len(v)} ≠ 配置 AICS_EMBED_DIM={self.dim}（模型 {self.model}）"
                )
        usage = data.get("usage") or {}
        log.debug(
            "provider.embedded", model=self.model, n=len(texts), tokens=usage.get("total_tokens")
        )
        return vectors

    async def _batch(self, texts: list[str]) -> list[list[float]]:
        async with self._sem:
            return await call_with_resilience(
                lambda: self._post(texts), breaker=self.breaker, timeout_s=self.timeout_s
            )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not t or not t.strip() for t in texts):
            raise ValueError("不能向量化空文本")
        batches = [texts[i : i + self.batch_size] for i in range(0, len(texts), self.batch_size)]
        results = await asyncio.gather(*(self._batch(b) for b in batches))
        return [v for r in results for v in r]

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]
