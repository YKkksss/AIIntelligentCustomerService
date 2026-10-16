"""Chat LLM provider: OpenAI-compatible endpoints (DeepSeek by default) via LangChain.

`chat_model()` returns a LangChain `ChatOpenAI` so LangGraph nodes can stream tokens.
Retries use the OpenAI SDK's own exponential backoff (`max_retries=2`, matching the 2 retries of
模型供应商抽象 §3) and `timeout`=AICS_LLM_TIMEOUT_S; the circuit breaker is applied by the
rag layer around whole node calls (M2.3), because a streaming call cannot be wrapped by
`call_with_resilience` without buffering.
"""

from __future__ import annotations

from typing import Any

import httpx
import openai
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from aics.core.models import ModelRegistry

LLM_MAX_RETRIES = 2
# Keys callers may override per call (per-app llm_model / temperature / max_tokens …).
OVERRIDABLE = frozenset(
    {"model", "temperature", "max_tokens", "streaming", "timeout", "stop", "thinking"}
)


def is_upstream_failure(exc: BaseException) -> bool:
    """True for outages the circuit breaker should count (network, timeout, 429, 5xx).

    Caller-side errors (400 bad request, 401/403 auth, unsupported override, bugs) are not
    counted: they do not indicate an unhealthy upstream and must not open the breaker for
    every app.
    """
    if isinstance(exc, TimeoutError | httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        default_model: str,
        registry: ModelRegistry,
        timeout_s: float = 60.0,
        max_retries: int = LLM_MAX_RETRIES,
    ) -> None:
        registry.require(default_model, "chat", field="AICS_LLM_MODEL")
        self.base_url = base_url
        self._api_key = SecretStr(api_key)
        self.default_model = default_model
        self.registry = registry
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def chat_model(self, **overrides: Any) -> ChatOpenAI:
        unknown = set(overrides) - OVERRIDABLE
        if unknown:
            raise ValueError(f"unsupported chat_model overrides: {sorted(unknown)}")
        model = overrides.pop("model", None) or self.default_model
        # Per-app models must be registered chat models (app config is validated on save too).
        spec = self.registry.require(model, "chat", field="llm_model")
        timeout = overrides.pop("timeout", self.timeout_s)
        thinking = overrides.pop("thinking", None)
        if thinking is not None and spec.thinking == "toggle":
            # DeepSeek-style switch; models without one ignore the request (registry decides).
            overrides["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        return ChatOpenAI(
            model=model,
            base_url=self.base_url,
            api_key=self._api_key,
            timeout=timeout,
            max_retries=self.max_retries,
            **overrides,
        )
