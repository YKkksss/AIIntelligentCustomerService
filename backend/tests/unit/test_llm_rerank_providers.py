"""M2.2: LLM and rerank providers (no network: respx mocks)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from aics.core.config import get_settings
from aics.providers import (
    DashScopeReranker,
    LlamaCppReranker,
    NoopReranker,
    OpenAICompatibleLLM,
    UpstreamError,
    UpstreamUnavailableError,
    build_llm,
    build_reranker,
)
from aics.providers.rerank import parse_results
from aics.providers.resilience import CircuitBreaker

DS_URL = "https://rerank.test/text-rerank"
LC_URL = "http://llama.test/v1/rerank"


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    import aics.providers.resilience as r

    orig = r.call_with_resilience

    async def fast(fn, **kw):  # type: ignore[no-untyped-def]
        kw["backoff"] = (0.0, 0.0)
        return await orig(fn, **kw)

    monkeypatch.setattr("aics.providers.rerank.call_with_resilience", fast)


def ds_ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    docs = body["input"]["documents"]
    # score = length of document, so the longest ranks first
    results = [{"index": i, "relevance_score": len(d) / 100} for i, d in enumerate(docs)]
    return httpx.Response(200, json={"output": {"results": results}, "usage": {"total_tokens": 9}})


@respx.mock
async def test_dashscope_rerank_sorted_and_payload() -> None:
    route = respx.post(DS_URL).mock(side_effect=ds_ok)
    rr = DashScopeReranker(url=DS_URL, model="qwen3.7-text-rerank", api_key="sk-x")
    out = await rr.rerank("退货", ["a", "aaa", "aa"], top_n=2)
    assert out == [(1, 0.03), (2, 0.02)]
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer sk-x"
    body = json.loads(req.content)
    assert body["model"] == "qwen3.7-text-rerank" and body["parameters"]["top_n"] == 2


@respx.mock
async def test_llamacpp_rerank() -> None:
    respx.post(LC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.1},
                    {"index": 1, "relevance_score": 0.9},
                ]
            },
        )
    )
    rr = LlamaCppReranker(url=LC_URL, model="qwen3-reranker", api_key=None)
    assert await rr.rerank("q", ["x", "y"], top_n=5) == [(1, 0.9), (0, 0.1)]


async def test_empty_docs_no_call_and_bad_args() -> None:
    rr = DashScopeReranker(url=DS_URL, model="m", api_key="k")
    assert await rr.rerank("q", [], top_n=3) == []
    with pytest.raises(ValueError):
        await rr.rerank("q", ["a"], top_n=0)
    with pytest.raises(ValueError):
        await rr.rerank("  ", ["a"], top_n=1)
    with pytest.raises(ValueError):
        await rr.rerank("q", ["a"] * 501, top_n=1)


@respx.mock
async def test_retry_then_unavailable_and_4xx_not_retried() -> None:
    route = respx.post(DS_URL).mock(return_value=httpx.Response(503))
    rr = DashScopeReranker(url=DS_URL, model="m", api_key="k", breaker=CircuitBreaker("t"))
    with pytest.raises(UpstreamUnavailableError):
        await rr.rerank("q", ["a"], top_n=1)
    assert route.call_count == 3  # 1 + 2 retries
    route.mock(return_value=httpx.Response(400))
    before = route.call_count
    with pytest.raises(UpstreamError):
        await rr.rerank("q", ["a"], top_n=1)
    assert route.call_count - before == 1


@respx.mock
async def test_malformed_response_rejected() -> None:
    respx.post(DS_URL).mock(return_value=httpx.Response(200, json={"output": {}}))
    rr = DashScopeReranker(url=DS_URL, model="m", api_key="k")
    with pytest.raises(UpstreamError):
        await rr.rerank("q", ["a"], top_n=1)


def test_parse_results_validation() -> None:
    assert parse_results([{"index": 0, "relevance_score": "0.5"}], 1, 5) == [(0, 0.5)]
    for bad in (
        [{"index": 3, "relevance_score": 0.1}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}],
        [{"index": 0}],
    ):
        with pytest.raises(UpstreamError):
            parse_results(bad, 2, 5)


async def test_noop_keeps_order() -> None:
    rr = NoopReranker()
    out = await rr.rerank("q", ["a", "b", "c"], top_n=2)
    assert [i for i, _ in out] == [0, 1] and rr.is_noop


def test_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    assert isinstance(build_reranker(s), DashScopeReranker)
    monkeypatch.setenv("AICS_RERANK_PROVIDER", "none")
    get_settings.cache_clear()
    assert isinstance(build_reranker(get_settings()), NoopReranker)
    llm = build_llm(s)
    assert isinstance(llm, OpenAICompatibleLLM)


def test_llm_chat_model_overrides() -> None:
    llm = build_llm(get_settings())
    m = llm.chat_model(temperature=0.2, max_tokens=800, streaming=True)
    assert m.model_name == "deepseek-flash" and m.temperature == 0.2 and m.max_tokens == 800
    assert m.max_retries == 2 and m.streaming
    assert "sk" not in repr(m.openai_api_key)  # secret stays masked
    with pytest.raises(ValueError):
        llm.chat_model(model="qwen3.7-text-rerank")  # not a chat model
    with pytest.raises(ValueError):
        llm.chat_model(api_key="leak")
