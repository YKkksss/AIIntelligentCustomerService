"""模型供应商抽象与实现（L1，只依赖 core）。工厂按 AICS_*_PROVIDER 选择实现。"""

from __future__ import annotations

from functools import lru_cache

from aics.core.config import Settings, get_settings
from aics.providers.base import (
    DimensionMismatchError,
    EmbeddingProvider,
    LLMProvider,
    ProviderError,
    RerankProvider,
    UpstreamError,
    UpstreamUnavailableError,
)
from aics.providers.embedding import OpenAICompatibleEmbedder
from aics.providers.llm import OpenAICompatibleLLM
from aics.providers.rerank import DashScopeReranker, LlamaCppReranker, NoopReranker

__all__ = [
    "DashScopeReranker",
    "DimensionMismatchError",
    "EmbeddingProvider",
    "LLMProvider",
    "LlamaCppReranker",
    "NoopReranker",
    "OpenAICompatibleEmbedder",
    "OpenAICompatibleLLM",
    "ProviderError",
    "RerankProvider",
    "UpstreamError",
    "UpstreamUnavailableError",
    "build_embedder",
    "build_llm",
    "build_reranker",
    "get_embedder",
    "get_llm",
    "get_reranker",
]


def build_embedder(s: Settings) -> EmbeddingProvider:
    # 模型必须已注册且 kind=embedding（启动时已校验，这里再取一次保证工厂独立可用）
    s.models.require(s.embed_model, "embedding", field="AICS_EMBED_MODEL")
    if s.embed_provider in ("dashscope", "openai_compatible"):
        return OpenAICompatibleEmbedder(
            base_url=s.embed_base_url,
            api_key=s.embed_api_key.get_secret_value(),
            model=s.embed_model,
            dim=s.embed_dim,
            batch_size=s.embed_batch,
            provider=s.embed_provider,
        )
    raise ValueError(f"不支持的 AICS_EMBED_PROVIDER={s.embed_provider}")  # pragma: no cover


@lru_cache(maxsize=1)
def get_embedder() -> EmbeddingProvider:
    return build_embedder(get_settings())


def build_llm(s: Settings) -> LLMProvider:
    if s.llm_provider == "openai_compatible":
        return OpenAICompatibleLLM(
            base_url=s.llm_base_url,
            api_key=s.llm_api_key.get_secret_value(),
            default_model=s.llm_model,
            registry=s.models,
            timeout_s=s.llm_timeout_s,
        )
    raise ValueError(f"不支持的 AICS_LLM_PROVIDER={s.llm_provider}")  # pragma: no cover


def build_reranker(s: Settings) -> RerankProvider:
    if s.rerank_provider == "none":
        return NoopReranker()
    s.models.require(s.rerank_model, "rerank", field="AICS_RERANK_MODEL")
    key = s.rerank_api_key.get_secret_value() if s.rerank_api_key else None
    if s.rerank_provider == "dashscope":
        if not key:
            raise ValueError("AICS_RERANK_PROVIDER=dashscope 时必须设置 AICS_RERANK_API_KEY")
        return DashScopeReranker(url=s.rerank_url, model=s.rerank_model, api_key=key)
    if s.rerank_provider == "llamacpp":
        return LlamaCppReranker(url=s.rerank_url, model=s.rerank_model, api_key=key)
    raise ValueError(f"不支持的 AICS_RERANK_PROVIDER={s.rerank_provider}")  # pragma: no cover


@lru_cache(maxsize=1)
def get_llm() -> LLMProvider:
    return build_llm(get_settings())


@lru_cache(maxsize=1)
def get_reranker() -> RerankProvider:
    return build_reranker(get_settings())
