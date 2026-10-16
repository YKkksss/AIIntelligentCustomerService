"""模型供应商接口与异常（docs/02-architecture/05-模型供应商抽象.md §1）。

实现：embedding（M1.1）、llm / rerank（M2.2）。工厂函数在 `aics.providers` 包中。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """供应商调用失败的基类。消息中不得包含 API Key。"""


class UpstreamError(ProviderError):
    """上游返回错误。`retryable` 为 True 时（429 / 5xx / 网络错误）由 resilience 重试。"""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class UpstreamUnavailableError(ProviderError):
    """熔断打开或重试耗尽，对外映射为 503 UPSTREAM_UNAVAILABLE。"""


class DimensionMismatchError(ProviderError):
    """embedding 返回维度与配置不一致：立即失败，防止写入错误集合。"""


@runtime_checkable
class EmbeddingProvider(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class RerankProvider(Protocol):
    model: str

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]: ...


@runtime_checkable
class LLMProvider(Protocol):
    # Returns a LangChain BaseChatModel (kept as Any so that importing the protocol does not
    # require langchain; the OpenAI-compatible implementation returns ChatOpenAI).
    def chat_model(self, **overrides: Any) -> Any: ...
