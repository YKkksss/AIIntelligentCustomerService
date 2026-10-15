"""Qdrant 封装（L1，只依赖 core）。所有访问强制 app_id 过滤，集合名统一带 `aics_` 前缀。"""

from __future__ import annotations

from functools import lru_cache

from qdrant_client import AsyncQdrantClient

from aics.core.config import Settings, get_settings
from aics.vectorstore.qdrant import (
    ChunkHit,
    ChunkPoint,
    QdrantVectorStore,
    VectorStoreError,
    collection_name,
)

__all__ = [
    "ChunkHit",
    "ChunkPoint",
    "QdrantVectorStore",
    "VectorStoreError",
    "build_vector_store",
    "collection_name",
    "get_vector_store",
]

QDRANT_TIMEOUT_S = 10


def build_vector_store(s: Settings) -> QdrantVectorStore:
    client = AsyncQdrantClient(
        url=s.qdrant_url,
        api_key=s.qdrant_api_key.get_secret_value(),
        timeout=QDRANT_TIMEOUT_S,
        prefer_grpc=False,
        # 共用 KnowledgeFlow 的实例版本由对方维护；客户端与服务端次版本差 1 以内兼容
        check_compatibility=False,
    )
    return QdrantVectorStore(
        client, prefix=s.qdrant_collection_prefix, embed_model=s.embed_model, dim=s.embed_dim
    )


@lru_cache(maxsize=1)
def get_vector_store() -> QdrantVectorStore:
    return build_vector_store(get_settings())
