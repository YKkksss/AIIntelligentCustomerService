import uuid
from collections.abc import AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient

from aics.vectorstore import (
    ChunkPoint,
    QdrantVectorStore,
    VectorStoreError,
    build_vector_store,
    collection_name,
)

A, B = uuid.uuid4(), uuid.uuid4()
DIM = 4


def vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


@pytest.fixture
async def store() -> AsyncIterator[QdrantVectorStore]:
    client = AsyncQdrantClient(location=":memory:")
    st = QdrantVectorStore(
        client, prefix="aics_test_", embed_model="qwen3.7-text-embedding-flash", dim=DIM
    )
    yield st
    await st.aclose()


def pts(doc: uuid.UUID, version: int, n: int, enabled: bool = True) -> list[ChunkPoint]:
    return [ChunkPoint(uuid.uuid4(), doc, version, vec(i), enabled) for i in range(n)]


def test_collection_name() -> None:
    assert (
        collection_name("aics_", "chunks", "qwen3.7-text-embedding-flash", 1024)
        == "aics_chunks__qwen3_7_text_embedding_flash__1024"
    )
    with pytest.raises(VectorStoreError):
        collection_name("kf_", "chunks", "m", 4)


async def test_ensure_idempotent_and_dim_check(store: QdrantVectorStore) -> None:
    await store.ensure_chunks_collection()
    store._ensured.clear()
    await store.ensure_chunks_collection()
    assert await store.client.collection_exists(store.chunks_collection)
    other = QdrantVectorStore(
        store.client, prefix="aics_test_", embed_model="qwen3.7-text-embedding-flash", dim=DIM
    )
    other.dim = 8  # 模拟配置改了维度但集合名未变
    with pytest.raises(VectorStoreError, match="维度"):
        await other.ensure_chunks_collection()


async def test_isolation_between_apps(store: QdrantVectorStore) -> None:
    da, db = uuid.uuid4(), uuid.uuid4()
    await store.upsert_chunks(A, pts(da, 1, 3))
    await store.upsert_chunks(B, pts(db, 1, 3))
    hits = await store.search_chunks(A, vec(0), limit=10)
    assert hits and all(h.document_id == da for h in hits)
    assert await store.count_chunks(A) == 3 and await store.count_chunks(B) == 3
    assert await store.search_chunks(uuid.uuid4(), vec(0), limit=10) == []


async def test_enabled_filter_and_set_enabled(store: QdrantVectorStore) -> None:
    d = uuid.uuid4()
    points = pts(d, 1, 2) + pts(d, 1, 1, enabled=False)
    await store.upsert_chunks(A, points)
    assert await store.count_chunks(A) == 2
    assert await store.count_chunks(A, enabled_only=False) == 3
    await store.set_enabled(A, [points[0].chunk_id], False)
    assert await store.count_chunks(A) == 1
    await store.set_enabled(B, [points[1].chunk_id], False)  # 别的应用改不动
    assert await store.count_chunks(A) == 1
    ids = {h.chunk_id for h in await store.search_chunks(A, vec(1), limit=10)}
    assert ids == {points[1].chunk_id}


async def test_delete_versions_document_app(store: QdrantVectorStore) -> None:
    d1, d2 = uuid.uuid4(), uuid.uuid4()
    await store.upsert_chunks(A, pts(d1, 1, 2) + pts(d1, 2, 2) + pts(d1, 3, 1) + pts(d2, 1, 2))
    await store.upsert_chunks(B, pts(d1, 1, 2))  # 同一 document_id 也不会被 A 的删除波及
    await store.delete_document(A, d1, below_index_version=3)
    assert await store.count_chunks(A) == 3
    await store.delete_document(A, d1, index_version=3)
    assert await store.count_chunks(A) == 2
    await store.delete_document(A, d2)
    assert await store.count_chunks(A) == 0
    assert await store.count_chunks(B) == 2
    await store.delete_app(B)
    assert await store.count_chunks(B) == 0
    with pytest.raises(ValueError):
        await store.delete_document(A, d1, index_version=1, below_index_version=2)


async def test_guards(store: QdrantVectorStore) -> None:
    with pytest.raises(VectorStoreError):
        await store.upsert_chunks(A, [ChunkPoint(uuid.uuid4(), uuid.uuid4(), 1, [1.0, 0.0])])
    with pytest.raises(VectorStoreError):
        await store.search_chunks(A, [1.0], limit=1)
    assert await store.upsert_chunks(A, []) == 0
    # 集合不存在时读与删都安全
    assert await store.search_chunks(A, vec(0), limit=3) == []
    assert await store.count_chunks(A) == 0
    await store.delete_app(A)


async def test_drop_collection(store: QdrantVectorStore) -> None:
    await store.upsert_chunks(A, pts(uuid.uuid4(), 1, 1))
    await store.drop_chunks_collection()
    assert not await store.client.collection_exists(store.chunks_collection)


def test_search_requires_app_id_signature() -> None:
    import inspect

    for name in [
        "upsert_chunks",
        "set_enabled",
        "search_chunks",
        "count_chunks",
        "delete_document",
        "delete_app",
    ]:
        params = list(inspect.signature(getattr(QdrantVectorStore, name)).parameters)
        assert params[1] == "app_id", name


def test_factory_and_prefix_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from aics.core.config import Settings, get_settings

    st = build_vector_store(get_settings())
    assert st.chunks_collection == "aics_chunks__qwen3_7_text_embedding_flash__1024"
    monkeypatch.setenv("AICS_QDRANT_COLLECTION_PREFIX", "kf_")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
