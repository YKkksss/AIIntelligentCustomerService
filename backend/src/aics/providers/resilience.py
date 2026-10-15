"""供应商调用的通用能力：超时、指数退避重试、熔断（模型供应商抽象 §3）。

- 重试：429 / 5xx / 网络错误 / 超时，退避 0.5s、1s，最多 2 次。
- 熔断：60s 窗口内失败率 > 50% 且调用 ≥ 10 次 → 打开 30s；打开期间直接失败；到期后放行一次试探。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence

import httpx
import structlog

from aics.providers.base import UpstreamError, UpstreamUnavailableError

log = structlog.get_logger()

DEFAULT_BACKOFF: tuple[float, ...] = (0.5, 1.0)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        window_s: float = 60.0,
        failure_ratio: float = 0.5,
        min_calls: int = 10,
        open_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.window_s, self.failure_ratio = window_s, failure_ratio
        self.min_calls, self.open_s = min_calls, open_s
        self._clock = clock
        self._calls: deque[tuple[float, bool]] = deque()
        self._opened_at: float | None = None

    def _trim(self, now: float) -> None:
        while self._calls and now - self._calls[0][0] > self.window_s:
            self._calls.popleft()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        # 到期后进入半开：放行试探
        return self._clock() - self._opened_at < self.open_s

    def allow(self) -> bool:
        return not self.is_open

    def record(self, ok: bool) -> None:
        now = self._clock()
        if self._opened_at is not None and now - self._opened_at >= self.open_s:
            # 半开试探的结果决定关闭或重新打开
            self._opened_at = None if ok else now
            self._calls.clear()
            if ok:
                log.info("provider.breaker_closed", breaker=self.name)
            return
        self._calls.append((now, ok))
        self._trim(now)
        total = len(self._calls)
        failures = sum(1 for _, s in self._calls if not s)
        if total >= self.min_calls and failures / total > self.failure_ratio:
            self._opened_at = now
            log.warning(
                "provider.breaker_opened", breaker=self.name, calls=total, failures=failures
            )


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, UpstreamError):
        return exc.retryable
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError | TimeoutError)


def status_retryable(status: int) -> bool:
    return status == 429 or status >= 500


async def call_with_resilience[T](
    fn: Callable[[], Awaitable[T]],
    *,
    breaker: CircuitBreaker,
    timeout_s: float,
    backoff: Sequence[float] = DEFAULT_BACKOFF,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """执行一次逻辑调用（含重试）；熔断器按逻辑调用的最终结果计数。"""
    if not breaker.allow():
        raise UpstreamUnavailableError(f"{breaker.name} 熔断中")
    attempt = 0
    while True:
        try:
            result = await asyncio.wait_for(fn(), timeout=timeout_s)
        except Exception as exc:
            if is_retryable(exc) and attempt < len(backoff):
                delay = backoff[attempt]
                attempt += 1
                log.warning(
                    "provider.retry",
                    breaker=breaker.name,
                    attempt=attempt,
                    delay_s=delay,
                    error_type=type(exc).__name__,
                )
                await sleep(delay)
                continue
            if is_retryable(exc):
                # 只有上游/网络类失败计入熔断；4xx、维度错误等是调用方问题，不应打开熔断
                breaker.record(False)
                raise UpstreamUnavailableError(f"{breaker.name} 重试耗尽") from exc
            raise
        breaker.record(True)
        return result
