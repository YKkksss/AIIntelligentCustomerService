"""M2.2b: document tags → Qdrant payload + PG filter; filtered retrieval never crosses tags."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from ingest_support import DIM, MD, FakeEmbedder
from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete

from aics.core.config import get_settings
from aics.core.errors import ValidationAppError
from aics.db.models import Chunk, ChunkParent, Document, Job
from aics.ingest.service import IngestService
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.modules.knowledge.service import KnowledgeService, normalize_tags
from aics.rag.config import AppRagConfig
from aics.rag.retrieval import EmbeddingVectorSearch, run_retrieval
from aics.storage.local import LocalStorage
from aics.vectorstore import QdrantVectorStore

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "docs"


@pytest.fixture
async def two_docs(pg: dict[str, Any], tmp_path: Path) -> Any:
    app_id = pg["app_a"]
    client = AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(client, prefix="aics_test_", embed_model="fake", dim=DIM)
    storage = LocalStorage(tmp_path)
    queue: list[tuple[str, list[Any]]] = []
    async with pg["sessions"]() as s:
        ks = KnowledgeService(
            s, storage=storage, store=store, enqueue=lambda t, a: queue.append((t, a))
        )
        a = await ks.upload_file(app_id, (FIXTURES / "sample.md").read_bytes(), "policy.md")
        b = await ks.upload_file(app_id, MD, "other.md")
        await s.commit()
        # tags set before the build → written into the points at upsert time
        await ks.set_document_tags(app_id, a.document.id, [" 售后 ", "售后", "cn"])
        await ks.set_document_tags(app_id, b.document.id, ["other"])
    svc = IngestService(
        pg["sessions"],
        storage=storage,
        store=store,
        embedder=FakeEmbedder(),
        settings=get_settings(),
        enqueue_cleanup=lambda *a: None,
    )
    for _, args in queue:
        assert await svc.run_build(uuid.UUID(args[0]), uuid.UUID(args[1])) == "done"
    yield {
        "app": app_id,
        "a": a.document.id,
        "b": b.document.id,
        "store": store,
        "sessions": pg["sessions"],
        "storage": storage,
        "vector": EmbeddingVectorSearch(FakeEmbedder(), store),
        "chunks": PgChunkSearch(pg["sessions"]),
    }
    async with pg["sessions"]() as s:
        for m in (Chunk, ChunkParent, Job, Document):
            await s.execute(delete(m).where(m.app_id == app_id))
        await s.commit()
    await client.close()


async def _docs(env: dict[str, Any], cfg: AppRagConfig, tags: list[str] | None) -> set[uuid.UUID]:
    res = await run_retrieval(
        env["app"],
        "退货 运费 规则",
        cfg,
        vector=env["vector"],
        chunk_search=env["chunks"],
        reranker=None,
        tags=tags,
    )
    return {c.chunk.document_id for c in res.fused}


async def test_tag_filter_both_routes(two_docs: dict[str, Any]) -> None:
    env = two_docs
    assert await _docs(env, AppRagConfig(), None) == {env["a"], env["b"]}
    assert await _docs(env, AppRagConfig(), ["other"]) == {env["b"]}
    assert await _docs(env, AppRagConfig(), ["售后", "nope"]) == {env["a"]}
    assert await _docs(env, AppRagConfig(), ["nope"]) == set()
    # each route individually respects the filter (vector = Qdrant payload, fulltext = PG)
    for mode in ("vector", "fulltext"):
        assert await _docs(env, AppRagConfig(retrieval_mode=mode), ["other"]) <= {env["b"]}
    # app default filter applies when the request has none; [] disables it
    cfg = AppRagConfig(default_tags=["cn"])
    assert await _docs(env, cfg, None) == {env["a"]}
    assert await _docs(env, cfg, []) == {env["a"], env["b"]}


async def test_tag_edit_after_build_syncs_qdrant(two_docs: dict[str, Any]) -> None:
    env = two_docs
    async with env["sessions"]() as s:
        ks = KnowledgeService(
            s, storage=env["storage"], store=env["store"], enqueue=lambda *a: None
        )
        doc = await ks.set_document_tags(env["app"], env["b"], ["售后"])
        assert doc.tags == ["售后"]
    vec = await FakeEmbedder().embed_query("x")
    hits = await env["store"].search_chunks(env["app"], vec, 100, tags=["售后"])
    assert {h.document_id for h in hits} == {env["a"], env["b"]}
    assert await env["store"].search_chunks(env["app"], vec, 100, tags=["other"]) == []
    assert await _docs(env, AppRagConfig(), ["other"]) == set()


def test_normalize_tags() -> None:
    assert normalize_tags([" a", "a", "", "b "]) == ["a", "b"]
    for bad in ([str(i) for i in range(21)], ["长" * 51], "abc"):
        with pytest.raises(ValidationAppError):
            normalize_tags(bad)  # type: ignore[arg-type]
