"""M2.2b: supplementary chunk questions — add / search / delete / rebuild migration."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from ingest_support import DIM, FakeEmbedder
from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete, select

from aics.core.config import get_settings
from aics.core.errors import ValidationAppError
from aics.db.models import Chunk, ChunkParent, ChunkQuestion, Document, Job
from aics.ingest.service import IngestService
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.modules.knowledge.service import (
    ChunkNotFoundError,
    KnowledgeService,
    QuestionNotFoundError,
)
from aics.rag.config import AppRagConfig
from aics.rag.retrieval import EmbeddingVectorSearch, run_retrieval
from aics.storage.local import LocalStorage
from aics.vectorstore import QdrantVectorStore

pytestmark = pytest.mark.integration
SAMPLE = (Path(__file__).resolve().parents[1] / "fixtures" / "docs" / "sample.md").read_bytes()
NEEDLE = "运费由买家承担"
# FakeEmbedder hashes text: a query equal to the question gets cosine 1.0 with its point
Q = "寄回去的快递费谁出呀"


class Env:
    def __init__(self, pg: dict[str, Any], tmp: Path) -> None:
        self.pg, self.app = pg, pg["app_a"]
        self.client = AsyncQdrantClient(location=":memory:")
        self.store = QdrantVectorStore(
            self.client, prefix="aics_test_", embed_model="fake", dim=DIM
        )
        self.storage = LocalStorage(tmp)
        self.embedder = FakeEmbedder()
        self.queue: list[tuple[str, list[Any]]] = []
        self.ingest = IngestService(
            pg["sessions"],
            storage=self.storage,
            store=self.store,
            embedder=self.embedder,
            settings=get_settings(),
            enqueue_cleanup=self._cleanup,
        )
        self.cleanups: list[tuple[Any, ...]] = []

    def _cleanup(self, *args: Any) -> None:
        self.cleanups.append(args)

    def ks(self, s: Any) -> KnowledgeService:
        return KnowledgeService(
            s,
            storage=self.storage,
            store=self.store,
            embedder=self.embedder,
            enqueue=lambda t, a: self.queue.append((t, a)),
        )

    async def drain(self) -> None:
        while self.queue:
            task, args = self.queue.pop(0)
            if task == "aics.ingest_document":
                assert await self.ingest.run_build(uuid.UUID(args[0]), uuid.UUID(args[1])) == "done"
        for doc_id, n, old_key in self.cleanups:
            await self.ingest.run_cleanup(doc_id, below_index_version=n, old_storage_key=old_key)
        self.cleanups.clear()

    async def needle_chunk(self) -> Chunk:
        async with self.pg["sessions"]() as s:
            return (
                await s.execute(
                    select(Chunk)
                    .join(Document, Document.id == Chunk.document_id)
                    .where(
                        Chunk.app_id == self.app,
                        Chunk.text.contains(NEEDLE),
                        Chunk.index_version == Document.index_version,
                    )
                )
            ).scalar_one()

    async def top_hit(self, query: str, *, questions: bool = True) -> tuple[Any, float]:
        hits = await EmbeddingVectorSearch(
            self.embedder, self.store, include_questions=questions
        ).search(self.app, query, 20)
        return hits[0]

    async def top(self, query: str, *, questions: bool = True) -> uuid.UUID | None:
        res = await run_retrieval(
            self.app,
            query,
            AppRagConfig(retrieval_mode="vector"),
            vector=EmbeddingVectorSearch(self.embedder, self.store, include_questions=questions),
            chunk_search=PgChunkSearch(self.pg["sessions"]),
            reranker=None,
        )
        return res.fused[0].chunk.chunk_id if res.fused else None


@pytest.fixture
async def env(pg: dict[str, Any], tmp_path: Path) -> Any:
    e = Env(pg, tmp_path)
    async with pg["sessions"]() as s:
        e.doc = (await e.ks(s).upload_file(e.app, SAMPLE, "sample.md")).document.id
        await s.commit()
    await e.drain()
    yield e
    async with pg["sessions"]() as s:
        for m in (ChunkQuestion, Chunk, ChunkParent, Job, Document):
            await s.execute(delete(m).where(m.app_id == e.app))
        await s.commit()
    await e.client.close()


async def test_question_recalls_its_chunk_and_delete_removes(env: Env) -> None:
    chunk = await env.needle_chunk()
    before_count = await env.store.count_chunks(env.app)
    assert (await env.top_hit(Q))[1] < 0.9999  # no point equals the query yet
    async with env.pg["sessions"]() as s:
        rows = await env.ks(s).add_chunk_questions(env.app, chunk.id, [f" {Q} ", Q, "退货邮费"])
    assert [r.question for r in rows] == [Q, "退货邮费"]
    assert await env.top(Q) == chunk.id
    assert (await env.top_hit(Q)) == (chunk.id, pytest.approx(1.0))
    assert (await env.top_hit(Q, questions=False))[1] < 0.9999  # chunk-only baseline
    # question points never count as chunks (reconcile)
    assert await env.store.count_chunks(env.app) == before_count
    assert sum((await env.store.count_by_document(env.app)).values()) == before_count
    # re-adding is idempotent
    async with env.pg["sessions"]() as s:
        assert await env.ks(s).add_chunk_questions(env.app, chunk.id, [Q]) == []
        assert len(await env.ks(s).list_chunk_questions(env.app, chunk.id)) == 2
        await env.ks(s).delete_chunk_question(env.app, rows[0].id)
        with pytest.raises(QuestionNotFoundError):
            await env.ks(s).delete_chunk_question(env.app, rows[0].id)
    assert (await env.top_hit(Q))[1] < 0.9999


async def test_disabling_chunk_disables_its_questions(env: Env) -> None:
    chunk = await env.needle_chunk()
    async with env.pg["sessions"]() as s:
        await env.ks(s).add_chunk_questions(env.app, chunk.id, [Q])
    await env.store.set_enabled(env.app, [chunk.id], False)
    vec = await env.embedder.embed_query(Q)
    hits = await env.store.search_chunks(env.app, vec, 50)
    assert chunk.id not in {h.chunk_id for h in hits}


async def test_rebuild_migrates_questions(env: Env) -> None:
    old = await env.needle_chunk()
    async with env.pg["sessions"]() as s:
        (q,) = await env.ks(s).add_chunk_questions(env.app, old.id, [Q])
        await env.ks(s).rebuild_document(env.app, env.doc)
    await env.drain()  # build → switch → migrate → cleanup of the old build
    new = await env.needle_chunk()
    assert new.id != old.id
    async with env.pg["sessions"]() as s:
        row = await s.get(ChunkQuestion, q.id)
        assert row is not None and row.chunk_id == new.id and row.status == "active"
    assert await env.top(Q) == new.id

    # replace with content lacking the chunk → question pending, its point removed
    changed = SAMPLE.replace(NEEDLE.encode(), "运费规则另行公告".encode())
    async with env.pg["sessions"]() as s:
        await env.ks(s).replace_file(env.app, env.doc, changed, "sample.md")
    await env.drain()
    async with env.pg["sessions"]() as s:
        row = await s.get(ChunkQuestion, q.id)
        assert row is not None and row.status == "pending" and row.chunk_id is None
        pend = await env.ks(s).list_document_questions(env.app, env.doc, status="pending")
        assert [p.id for p in pend] == [q.id]
    vec = await env.embedder.embed_query(Q)
    assert q.id not in {h.point_id for h in await env.store.search_chunks(env.app, vec, 100)}

    # admin re-attaches it to a live chunk
    async with env.pg["sessions"]() as s:
        target = (
            (
                await s.execute(
                    select(Chunk)
                    .join(Document, Document.id == Chunk.document_id)
                    .where(Chunk.app_id == env.app, Chunk.index_version == Document.index_version)
                    .order_by(Chunk.ordinal)
                )
            )
            .scalars()
            .first()
        )
        r = await env.ks(s).reattach_chunk_question(env.app, q.id, target.id)
        assert r.status == "active" and r.chunk_id == target.id
    assert await env.top(Q) == target.id


async def test_validation(env: Env, pg: dict[str, Any]) -> None:
    chunk = await env.needle_chunk()
    async with env.pg["sessions"]() as s:
        ks = env.ks(s)
        for bad in ([], ["  "], ["长" * 201], "abc"):
            with pytest.raises(ValidationAppError):
                await ks.add_chunk_questions(env.app, chunk.id, bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationAppError):
            await ks.add_chunk_questions(env.app, chunk.id, [f"问题{i}" for i in range(21)])
        with pytest.raises(ValidationAppError):
            await ks.add_chunk_questions(env.app, chunk.id, ["x"], source="robot")
        with pytest.raises(ChunkNotFoundError):
            await ks.add_chunk_questions(env.app, uuid.uuid4(), ["x"])
        with pytest.raises(ChunkNotFoundError):  # other app cannot touch this chunk
            await ks.add_chunk_questions(pg["app_b"], chunk.id, ["x"])


class NeedleReranker:
    model = "fake"

    async def rerank(self, query: str, docs: list[str], top_n: int) -> list[tuple[int, float]]:
        return sorted(
            [(i, 0.95 if NEEDLE in d else 0.1) for i, d in enumerate(docs)], key=lambda x: -x[1]
        )[:top_n]


async def test_hit_test(env: Env) -> None:
    from sqlalchemy import func

    from aics.db.models import App, Conversation, Message

    async with env.pg["sessions"]() as s:
        before = (await s.get(App, env.app)).config_version
        ks = env.ks(s)
        r = await ks.hit_test(env.app, "退货运费谁承担", reranker=NeedleReranker())
        assert r["vector"] and r["keyword"]
        assert {"chunk_id", "score", "rank"} <= set(r["vector"][0])
        assert r["rerank_applied"] and NEEDLE in r["reranked"][0]["text"]
        assert r["reranked"][0]["rerank"] == 0.95 and r["rag_config"]["fusion"] == "weighted"
        w = await ks.hit_test(
            env.app,
            "退货运费谁承担",
            reranker=None,
            overrides={"rag_config": {"retrieval_mode": "fulltext", "fusion": "weighted"}},
        )
        assert w["vector"] == [] and w["keyword"] and not w["rerank_applied"]
        assert w["rag_config"]["retrieval_mode"] == "fulltext"
        nf = await ks.hit_test(env.app, "退货", reranker=None, filters={"tags": ["nope"]})
        assert nf["fused"] == [] and nf["tags"] == ["nope"]
        for kw in (
            {"overrides": {"rag_config": {"vector_weight": 2}}},
            {"overrides": {"llm": {}}},
            {"filters": {"doc": 1}},
        ):
            with pytest.raises(ValidationAppError):
                await ks.hit_test(env.app, "退货", reranker=None, **kw)
        with pytest.raises(ValidationAppError):
            await ks.hit_test(env.app, "  ", reranker=None)
        # read-only: no conversation / message rows, app config untouched
        for m in (Conversation, Message):
            n = await s.scalar(select(func.count()).select_from(m).where(m.app_id == env.app))
            assert n == 0
        await s.refresh(await s.get(App, env.app))
        assert (await s.get(App, env.app)).config_version == before
