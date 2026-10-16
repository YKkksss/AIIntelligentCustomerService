"""共用 Qdrant（服务器为 KnowledgeFlow 的 v1.18.2）上的集成测试：只创建 aics_test_ 前缀集合，结束后删除。

运行：AICS_TEST_QDRANT_URL=http://192.168.110.86:6333 AICS_TEST_QDRANT_API_KEY=<key> uv run pytest -m integration
"""

from __future__ import annotations

import os
import uuid

import pytest
from qdrant_client import AsyncQdrantClient

from aics.vectorstore import ChunkPoint, QdrantVectorStore

pytestmark = pytest.mark.integration

URL = os.environ.get("AICS_TEST_QDRANT_URL")
KEY = os.environ.get("AICS_TEST_QDRANT_API_KEY")


async def test_roundtrip_on_real_qdrant() -> None:
    if not URL or URL == ":memory:":
        pytest.skip("未设置服务器 AICS_TEST_QDRANT_URL（:memory: 本地模式不跑共用实例测试）")
    client = AsyncQdrantClient(url=URL, api_key=KEY, check_compatibility=False, timeout=10)
    model = f"it-{uuid.uuid4().hex[:8]}"
    st = QdrantVectorStore(client, prefix="aics_test_", embed_model=model, dim=8)
    assert st.chunks_collection.startswith("aics_test_chunks__")
    try:
        a, b, doc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        v = [1.0] + [0.0] * 7
        await st.upsert_chunks(a, [ChunkPoint(uuid.uuid4(), doc, 1, v)])
        await st.upsert_chunks(b, [ChunkPoint(uuid.uuid4(), doc, 1, v)])
        hits = await st.search_chunks(a, v, limit=5)
        assert len(hits) == 1 and hits[0].score > 0.99
        assert await st.count_chunks(b) == 1
        info = await client.get_collection(st.chunks_collection)
        assert info.config.quantization_config is not None
        assert {"app_id", "document_id", "index_version", "enabled"} <= set(info.payload_schema)
    finally:
        await st.drop_chunks_collection()
        await client.close()
