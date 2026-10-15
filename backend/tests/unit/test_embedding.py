import json

import httpx
import pytest
import respx

from aics.providers import (
    DimensionMismatchError,
    EmbeddingProvider,
    OpenAICompatibleEmbedder,
    UpstreamError,
    UpstreamUnavailableError,
    build_embedder,
    resilience,
)

BASE = "https://dashscope.example/compatible-mode/v1"
URL = BASE + "/embeddings"


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience, "DEFAULT_BACKOFF", (0.0, 0.0))
    import aics.providers.resilience as r

    orig = r.call_with_resilience

    async def fast(fn, **kw):  # type: ignore[no-untyped-def]
        kw.setdefault("backoff", (0.0, 0.0))
        return await orig(fn, **kw)

    monkeypatch.setattr("aics.providers.embedding.call_with_resilience", fast)


def ok_response(request: httpx.Request, dim: int = 4) -> httpx.Response:
    body = json.loads(request.content)
    data = [
        {"index": i, "embedding": [float(len(t))] + [0.0] * (dim - 1)}
        for i, t in enumerate(body["input"])
    ]
    return httpx.Response(200, json={"data": list(reversed(data)), "usage": {"total_tokens": 7}})


def make(**kw) -> OpenAICompatibleEmbedder:  # type: ignore[no-untyped-def]
    args = {"base_url": BASE, "api_key": "sk-test", "model": "m", "dim": 4, "batch_size": 10}
    args.update(kw)
    return OpenAICompatibleEmbedder(**args)


@respx.mock
async def test_batches_order_and_request_format() -> None:
    route = respx.post(URL).mock(side_effect=ok_response)
    emb = make()
    assert isinstance(emb, EmbeddingProvider)
    texts = [f"t{'x' * i}" for i in range(25)]
    vecs = await emb.embed_documents(texts)
    assert route.call_count == 3
    assert [v[0] for v in vecs] == [float(len(t)) for t in texts]  # 按 index 还原顺序
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "m" and body["dimensions"] == 4 and len(body["input"]) == 10


@respx.mock
async def test_embed_query_and_empty() -> None:
    respx.post(URL).mock(side_effect=ok_response)
    emb = make()
    assert await emb.embed_documents([]) == []
    assert len(await emb.embed_query("退货")) == 4
    with pytest.raises(ValueError):
        await emb.embed_documents(["ok", "  "])


@respx.mock
async def test_dimension_mismatch() -> None:
    respx.post(URL).mock(side_effect=lambda r: ok_response(r, dim=3))
    with pytest.raises(DimensionMismatchError):
        await make().embed_query("x")


@respx.mock
async def test_retry_on_429_then_ok() -> None:
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(503), ok_response]
    )
    assert len(await make().embed_query("x")) == 4
    assert route.call_count == 3


@respx.mock
async def test_400_not_retried() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(UpstreamError) as ei:
        await make().embed_query("x")
    assert ei.value.status == 400 and route.call_count == 1
    assert "sk-test" not in str(ei.value)


@respx.mock
async def test_exhausted_and_breaker_opens() -> None:
    respx.post(URL).mock(return_value=httpx.Response(500))
    emb = make()
    for _ in range(10):
        with pytest.raises(UpstreamUnavailableError):
            await emb.embed_query("x")
    assert emb.breaker.is_open


@respx.mock
async def test_count_mismatch() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"data": []}))
    with pytest.raises(UpstreamError):
        await make().embed_query("x")


def test_factory_uses_settings() -> None:
    from aics.core.config import get_settings

    emb = build_embedder(get_settings())
    assert emb.model == "qwen3.7-text-embedding-flash" and emb.dim == 1024
