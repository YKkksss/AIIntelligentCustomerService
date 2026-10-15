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

__all__ = [
    "DimensionMismatchError",
    "EmbeddingProvider",
    "LLMProvider",
    "OpenAICompatibleEmbedder",
    "ProviderError",
    "RerankProvider",
    "UpstreamError",
    "UpstreamUnavailableError",
    "build_embedder",
    "get_embedder",
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
