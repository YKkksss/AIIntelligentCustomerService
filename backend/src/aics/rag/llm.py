"""LLM access for graph nodes with a node-level circuit breaker (模型供应商抽象 §3, §4).

`providers.llm` retries inside the OpenAI SDK (`max_retries=2`) but cannot wrap a streaming
call with `call_with_resilience`, so the breaker lives here and counts one *node call*
(classify / judge / generate) as one logical call:

- breaker open → `LlmUnavailableError` (503 UPSTREAM_UNAVAILABLE) without calling upstream;
- an upstream outage (network, timeout, 429, 5xx — `providers.llm.is_upstream_failure`)
  records a failure; caller-side errors (400 / auth / bugs) raise without counting; a
  successful call (even if its output later fails JSON validation — a model-quality issue,
  not an outage) records success;
- cancellation (client disconnect) records nothing;
- a call that runs into its own *node budget* (`timeout_s`, e.g. classify 2 s with a kb_qa
  fallback) is a slow call, not an outage: it raises TimeoutError but records nothing. A slow
  upstream otherwise opens the model-wide breaker from classify alone and turns every
  following generate into 503 (design discussion 0007 D8; resilience4j keeps slow calls apart
  from failures for the same reason).

Short structured calls (classify / condense / judge) request `thinking=False`: on hybrid
reasoning models (registry `thinking: toggle`) this halves output tokens and latency
(design discussion 0004 §5). Breakers are process-wide, one per model name, with the shared defaults
(60 s window, > 50 % failures with ≥ 10 calls → open 30 s).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

from aics.core.errors import AppError
from aics.providers.base import LLMProvider
from aics.providers.llm import is_upstream_failure
from aics.providers.resilience import CircuitBreaker
from aics.rag.schemas import TokenUsage

log = structlog.get_logger()

JSON_MODE: dict[str, Any] = {"response_format": {"type": "json_object"}}


class LlmUnavailableError(AppError):
    code, status, message = "UPSTREAM_UNAVAILABLE", 503, "大模型服务暂不可用"


class LlmCallError(AppError):
    code, status, message = "UPSTREAM_ERROR", 502, "大模型调用失败"


_BREAKERS: dict[str, CircuitBreaker] = {}


# An exception this close to the node budget is the budget expiring (SDK timeout = budget).
BUDGET_HIT_RATIO = 0.95


def breaker_for(model: str) -> CircuitBreaker:
    """Process-wide breaker per chat model (shared by all apps / requests)."""
    b = _BREAKERS.get(model)
    if b is None:
        b = _BREAKERS[model] = CircuitBreaker(f"llm:{model}")
    return b


def reset_breakers() -> None:  # tests
    _BREAKERS.clear()


def usage_of(msg: AIMessage | AIMessageChunk | None) -> TokenUsage:
    meta = getattr(msg, "usage_metadata", None) or {}
    details = meta.get("output_token_details") or {}
    return TokenUsage(
        prompt_tokens=int(meta.get("input_tokens") or 0),
        completion_tokens=int(meta.get("output_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning") or 0),
        calls=1,
    )


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks
        return "".join(
            b if isinstance(b, str) else str(b.get("text", ""))
            for b in content
            if isinstance(b, str) or (isinstance(b, dict) and b.get("type") == "text")
        )
    return ""


@dataclass
class LlmReply:
    text: str
    usage: TokenUsage
    elapsed_ms: int


@dataclass
class StreamState:
    """Filled while `LlmGateway.stream` is consumed."""

    text: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    first_token_ms: int | None = None


class LlmGateway:
    """Thin wrapper over an `LLMProvider` (ChatOpenAI factory) used by rag nodes."""

    def __init__(self, provider: LLMProvider, *, breakers: dict[str, CircuitBreaker] | None = None):
        self.provider = provider
        self._breakers = breakers

    def model_name(self, override: str | None) -> str:
        return override or str(getattr(self.provider, "default_model", "") or "default")

    def breaker(self, model: str) -> CircuitBreaker:
        if self._breakers is not None:
            if model not in self._breakers:
                self._breakers[model] = CircuitBreaker(f"llm:{model}")
            return self._breakers[model]
        return breaker_for(model)

    def _admit(self, model: str, node: str) -> CircuitBreaker:
        b = self.breaker(model)
        if not b.allow():
            log.warning("rag.llm_breaker_open", node=node, model=model)
            raise LlmUnavailableError(details={"node": node})
        return b

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        *,
        node: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 200,
        timeout_s: float | None = None,
        json_mode: bool = False,
        thinking: bool | None = False,
    ) -> LlmReply:
        """One non-streaming call. Raises LlmUnavailableError / LlmCallError / TimeoutError."""
        name = self.model_name(model)
        b = self._admit(name, node)
        overrides: dict[str, Any] = {"temperature": temperature, "max_tokens": max_tokens}
        if model:
            overrides["model"] = model
        if timeout_s is not None:
            overrides["timeout"] = timeout_s
        if thinking is not None:
            overrides["thinking"] = thinking
        chat = self.provider.chat_model(**overrides)
        kwargs = JSON_MODE if json_mode else {}
        t0 = time.perf_counter()
        try:
            if timeout_s is not None:
                async with asyncio.timeout(timeout_s):
                    msg = await chat.ainvoke(list(messages), **kwargs)
            else:
                msg = await chat.ainvoke(list(messages), **kwargs)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if timeout_s is None:  # no node budget: a transport-level timeout is an outage
                b.record(False)
            log.warning("rag.llm_timeout", node=node, model=name, timeout_s=timeout_s)
            raise
        except AppError:
            raise
        except Exception as e:
            if timeout_s is not None and time.perf_counter() - t0 >= timeout_s * BUDGET_HIT_RATIO:
                # The SDK's own timeout (set to the same budget) fired first: same slow call.
                log.warning("rag.llm_timeout", node=node, model=name, timeout_s=timeout_s)
                raise TimeoutError(f"{node} exceeded {timeout_s}s") from e
            if is_upstream_failure(e):
                b.record(False)
            log.warning("rag.llm_failed", node=node, model=name, error_type=type(e).__name__)
            raise LlmCallError(details={"node": node, "error_type": type(e).__name__}) from e
        b.record(True)
        return LlmReply(_text(msg.content), usage_of(msg), int((time.perf_counter() - t0) * 1000))

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        state: StreamState,
        *,
        node: str,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 800,
        thinking: bool | None = None,
    ) -> AsyncIterator[str]:
        """Stream text deltas; `state` accumulates the full text and usage."""
        name = self.model_name(model)
        b = self._admit(name, node)
        overrides: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "streaming": True,
        }
        if model:
            overrides["model"] = model
        if thinking is not None:
            overrides["thinking"] = thinking
        chat = self.provider.chat_model(**overrides)
        t0 = time.perf_counter()
        last: AIMessageChunk | None = None
        try:
            async for chunk in chat.astream(list(messages), stream_usage=True):
                if getattr(chunk, "usage_metadata", None):
                    last = chunk
                piece = _text(chunk.content)
                if not piece:
                    continue
                if state.first_token_ms is None:
                    state.first_token_ms = int((time.perf_counter() - t0) * 1000)
                state.text += piece
                yield piece
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except AppError:
            raise
        except Exception as e:
            if is_upstream_failure(e):
                b.record(False)
            log.warning("rag.llm_failed", node=node, model=name, error_type=type(e).__name__)
            raise LlmCallError(details={"node": node, "error_type": type(e).__name__}) from e
        b.record(True)
        state.usage = usage_of(last) if last is not None else TokenUsage(calls=1)
