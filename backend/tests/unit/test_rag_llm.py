"""M2.3: LlmGateway node-level circuit breaker, usage accounting and errors."""

from __future__ import annotations

import time

import httpx
import pytest
from langchain_core.messages import HumanMessage

from aics.providers.resilience import CircuitBreaker
from aics.rag.llm import LlmCallError, LlmGateway, LlmUnavailableError, StreamState
from tests.rag_support import FakeLLMProvider


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


def gw(responder, **kw):  # type: ignore[no-untyped-def]
    clock = Clock()
    breakers = {"fake-chat": CircuitBreaker("llm:fake-chat", clock=clock)}
    return LlmGateway(FakeLLMProvider(responder, **kw), breakers=breakers), clock


MSG = [HumanMessage("hi")]


async def test_complete_usage_and_json_mode() -> None:
    g, _ = gw(lambda m, k: '{"ok":true}')
    r = await g.complete(MSG, node="judge", json_mode=True, max_tokens=50)
    assert r.text == '{"ok":true}' and r.usage.calls == 1 and r.usage.completion_tokens == 11
    prov = g.provider
    assert prov.calls[0]["kwargs"]["response_format"] == {"type": "json_object"}  # type: ignore[attr-defined]


async def test_stream_accumulates() -> None:
    g, _ = gw(lambda m, k: "abcdefghij")
    st = StreamState()
    pieces = [p async for p in g.stream(MSG, st, node="generate")]
    assert "".join(pieces) == st.text == "abcdefghij"
    assert st.usage.completion_tokens == 10 and st.first_token_ms is not None


async def test_breaker_opens_half_opens_and_closes() -> None:
    state = {"fail": True}

    def r(m, k):  # type: ignore[no-untyped-def]
        if state["fail"]:
            raise httpx.ConnectError("down")
        return "ok"

    g, clock = gw(r)
    for _ in range(10):
        with pytest.raises(LlmCallError):
            await g.complete(MSG, node="judge")
    with pytest.raises(LlmUnavailableError):
        await g.complete(MSG, node="judge")
    with pytest.raises(LlmUnavailableError):  # stream short-circuits too
        st = StreamState()
        async for _ in g.stream(MSG, st, node="generate"):
            pass
    clock.t = 31.0  # half-open: one probe allowed
    state["fail"] = False
    assert (await g.complete(MSG, node="judge")).text == "ok"
    assert g.breaker("fake-chat").allow()


async def test_node_budget_timeout_does_not_open_breaker() -> None:
    """0007 D8: classify running into its 2 s budget is a slow call, not an outage."""
    g, _ = gw(lambda m, k: "late", delay_s=0.2)
    for _ in range(15):
        with pytest.raises(TimeoutError):
            await g.complete(MSG, node="classify", timeout_s=0.01)
    b = g.breaker("fake-chat")
    assert not b._calls and b.allow()


async def test_sdk_timeout_at_budget_is_a_slow_call() -> None:
    def r(m, k):  # type: ignore[no-untyped-def]
        time.sleep(0.03)
        raise httpx.ConnectTimeout("budget")

    g, _ = gw(r)
    with pytest.raises(TimeoutError):
        await g.complete(MSG, node="classify", timeout_s=0.02)
    assert not g.breaker("fake-chat")._calls


def test_model_name() -> None:
    g, _ = gw(lambda m, k: "")
    assert g.model_name(None) == "fake-chat" and g.model_name("x") == "x"


async def test_caller_errors_do_not_open_breaker() -> None:
    def r(m, k):  # type: ignore[no-untyped-def]
        raise ValueError("bad request shape")

    g, _ = gw(r)
    for _ in range(15):
        with pytest.raises(LlmCallError):
            await g.complete(MSG, node="judge")
    assert g.breaker("fake-chat").allow()
