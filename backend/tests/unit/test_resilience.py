import httpx
import pytest

from aics.providers.base import UpstreamError, UpstreamUnavailableError
from aics.providers.resilience import CircuitBreaker, call_with_resilience


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def no_sleep(_: float) -> None:
    return None


def make_fn(results: list[object]):
    calls = {"n": 0}

    async def fn() -> str:
        r = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        if isinstance(r, BaseException):
            raise r
        return str(r)

    return fn, calls


async def test_retry_then_success() -> None:
    fn, calls = make_fn([UpstreamError("429", status=429, retryable=True), "ok"])
    br = CircuitBreaker("t")
    assert await call_with_resilience(fn, breaker=br, timeout_s=1, sleep=no_sleep) == "ok"
    assert calls["n"] == 2


async def test_retries_exhausted() -> None:
    fn, calls = make_fn([httpx.ConnectError("x")])
    sleeps: list[float] = []

    async def rec(d: float) -> None:
        sleeps.append(d)

    with pytest.raises(UpstreamUnavailableError):
        await call_with_resilience(fn, breaker=CircuitBreaker("t"), timeout_s=1, sleep=rec)
    assert calls["n"] == 3 and sleeps == [0.5, 1.0]


async def test_non_retryable_not_retried_nor_counted() -> None:
    fn, calls = make_fn([UpstreamError("400", status=400)])
    br = CircuitBreaker("t", min_calls=1)
    with pytest.raises(UpstreamError):
        await call_with_resilience(fn, breaker=br, timeout_s=1, sleep=no_sleep)
    assert calls["n"] == 1 and not br.is_open


async def test_timeout_is_retryable() -> None:
    import asyncio

    n = {"c": 0}

    async def slow() -> str:
        n["c"] += 1
        if n["c"] == 1:
            await asyncio.sleep(1)
        return "ok"

    res = await call_with_resilience(
        slow, breaker=CircuitBreaker("t"), timeout_s=0.01, sleep=no_sleep
    )
    assert res == "ok" and n["c"] == 2


def test_breaker_opens_and_half_opens() -> None:
    clock = Clock()
    br = CircuitBreaker("t", clock=clock)
    for _ in range(4):
        br.record(True)
    for _ in range(5):
        br.record(False)
    assert not br.is_open  # 9 次，未达 min_calls
    br.record(False)  # 10 次，6 失败 > 50%
    assert br.is_open
    clock.t += 31
    assert br.allow()  # 半开
    br.record(False)
    assert br.is_open  # 试探失败重新打开
    clock.t += 31
    br.record(True)
    assert not br.is_open and br.allow()


def test_breaker_window_expires() -> None:
    clock = Clock()
    br = CircuitBreaker("t", clock=clock)
    for _ in range(9):
        br.record(False)
    clock.t += 61
    br.record(False)  # 旧记录出窗口，只剩 1 次
    assert not br.is_open


async def test_open_breaker_fails_fast() -> None:
    br = CircuitBreaker("t", min_calls=1)
    br.record(False)
    fn, calls = make_fn(["ok"])
    with pytest.raises(UpstreamUnavailableError, match="熔断"):
        await call_with_resilience(fn, breaker=br, timeout_s=1, sleep=no_sleep)
    assert calls["n"] == 0
