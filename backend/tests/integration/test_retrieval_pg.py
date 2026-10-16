"""M2.2 (integration): retrieve → fuse → rerank on a fixed sample KB.

Real PG (keyword route + retrievable filter) and the real ingest pipeline; Qdrant runs in
qdrant-client local in-memory mode with a deterministic fake embedder (the shared Qdrant
needs credentials not available on every dev box; server re-verification is listed in the
dev plan).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from ingest_support import DIM, FakeEmbedder
from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete, select, update

from aics.core.config import get_settings
from aics.db.models import Chunk, ChunkParent, Document, Job
from aics.ingest.service import IngestService
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.modules.knowledge.service import KnowledgeService
from aics.providers import UpstreamUnavailableError
from aics.rag.config import AppRagConfig
from aics.rag.retrieval import EmbeddingVectorSearch, run_retrieval
from aics.storage.local import LocalStorage
from aics.vectorstore import QdrantVectorStore

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "docs"


class FailingReranker:
    model = "x"

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        raise UpstreamUnavailableError("down")


class ContainsReranker:
    """Scores 1.0 for documents containing the needle (stands in for a cross-encoder)."""

    model = "x"

    def __init__(self, needle: str) -> None:
        self.needle = needle

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        s = [(i, 1.0 if self.needle in d else 0.0) for i, d in enumerate(docs)]
        return sorted(s, key=lambda x: -x[1])[:top_n]


@pytest.fixture
async def kb(pg: dict[str, Any], tmp_path: Path) -> Any:
    app_id = pg["app_a"]
    client = AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(client, prefix="aics_test_", embed_model="fake", dim=DIM)
    storage = LocalStorage(tmp_path)
    embedder = FakeEmbedder()
    queue: list[tuple[str, list[Any]]] = []
    async with pg["sessions"]() as s:
        ks = KnowledgeService(
            s, storage=storage, store=store, enqueue=lambda t, a: queue.append((t, a))
        )
        await ks.upload_file(app_id, (FIXTURES / "sample.md").read_bytes(), "sample.md")
        await s.commit()
    svc = IngestService(
        pg["sessions"],
        storage=storage,
        store=store,
        embedder=embedder,
        settings=get_settings(),
        enqueue_cleanup=lambda *a: None,
    )
    for task, args in queue:
        assert task == "aics.ingest_document"
        assert await svc.run_build(uuid.UUID(args[0]), uuid.UUID(args[1])) == "done"
    yield {
        "app": app_id,
        "sessions": pg["sessions"],
        "vector": EmbeddingVectorSearch(embedder, store),
        "chunks": PgChunkSearch(pg["sessions"]),
        "store": store,
    }
    async with pg["sessions"]() as s:
        for m in (Chunk, ChunkParent, Job, Document):
            await s.execute(delete(m).where(m.app_id == app_id))
        await s.commit()
    await client.close()


async def _texts(res: Any) -> list[str]:
    return [c.text for c in res.reranked]


async def test_expected_chunk_returned(kb: dict[str, Any]) -> None:
    res = await run_retrieval(
        kb["app"],
        "非质量问题退货运费谁承担？",
        AppRagConfig(),
        vector=kb["vector"],
        chunk_search=kb["chunks"],
        reranker=ContainsReranker("运费由买家承担"),
    )
    assert res.rerank_applied and res.degraded == []
    assert "运费由买家承担" in res.reranked[0].text
    assert res.reranked[0].keyword_rank is not None and res.reranked[0].vector_rank is not None
    assert res.reranked[0].chunk.document_title


async def test_rerank_failure_falls_back_to_rrf(kb: dict[str, Any]) -> None:
    cfg = AppRagConfig(rerank_top_n=3)
    res = await run_retrieval(
        kb["app"],
        "退款多久到账",
        cfg,
        vector=kb["vector"],
        chunk_search=kb["chunks"],
        reranker=FailingReranker(),
    )
    assert res.degraded == ["rerank_failed"]
    assert [c.unit_id for c in res.reranked] == [c.unit_id for c in res.fused[:3]]
    assert all(c.rerank_score is None for c in res.reranked)


async def test_flagged_and_old_version_not_returned(kb: dict[str, Any]) -> None:
    async with kb["sessions"]() as s:
        target = (
            await s.execute(
                select(Chunk).where(
                    Chunk.app_id == kb["app"], Chunk.text.contains("运费由买家承担")
                )
            )
        ).scalar_one()
        # flag it (Qdrant point stays enabled on purpose: PG is the source of truth)
        await s.execute(update(Chunk).where(Chunk.id == target.id).values(review_status="flagged"))
        await s.commit()
    res = await run_retrieval(
        kb["app"],
        "非质量问题退货运费谁承担？",
        AppRagConfig(),
        vector=kb["vector"],
        chunk_search=kb["chunks"],
        reranker=None,
    )
    assert target.id not in {c.chunk.chunk_id for c in res.fused}

    async with kb["sessions"]() as s:
        await s.execute(
            update(Document).where(Document.app_id == kb["app"]).values(index_version=99)
        )
        await s.commit()
    res = await run_retrieval(
        kb["app"],
        "退货",
        AppRagConfig(),
        vector=kb["vector"],
        chunk_search=kb["chunks"],
        reranker=None,
    )
    assert res.fused == []  # every chunk now belongs to an old build


async def test_other_app_sees_nothing(kb: dict[str, Any], pg: dict[str, Any]) -> None:
    res = await run_retrieval(
        pg["app_b"],
        "退货运费",
        AppRagConfig(),
        vector=kb["vector"],
        chunk_search=kb["chunks"],
        reranker=None,
    )
    assert res.fused == [] and res.vector_hits == [] and res.keyword_hits == []
