"""M1.4 入库流水线：真实 PG + Qdrant（aics_test_ 前缀集合），嵌入器用确定性假实现。

运行：AICS_TEST_DB_URL=... AICS_TEST_QDRANT_URL=... AICS_TEST_QDRANT_API_KEY=... uv run pytest -m integration
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import App, Chunk, ChunkParent, Document, Job, User
from aics.ingest.service import IngestService
from aics.modules.apps.service import AppService
from aics.storage.local import LocalStorage
from aics.vectorstore import QdrantVectorStore

pytestmark = pytest.mark.integration
DB = os.environ.get("AICS_TEST_DB_URL")
QURL = os.environ.get("AICS_TEST_QDRANT_URL")
QKEY = os.environ.get("AICS_TEST_QDRANT_API_KEY")
DIM = 8
MD = ("# 售后\n\n## 退货\n\n" + "签收后七天内可无理由退货，运费规则见下。\n\n" * 40).encode()


class FakeEmbedder:
    model, dim = "fake", DIM

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call
        self.texts: list[str] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("embedding upstream down")
        self.texts += texts
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            out.append([b / 255 + 0.01 for b in h[:DIM]])
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


@pytest.fixture
def migrated(monkeypatch: pytest.MonkeyPatch) -> str:
    if not (DB and QURL):
        pytest.skip("未设置 AICS_TEST_DB_URL / AICS_TEST_QDRANT_URL")
    monkeypatch.setenv("AICS_DB_URL", DB)
    monkeypatch.setenv("AICS_EMBED_BATCH", "2")
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    return DB


@pytest.fixture
async def env(migrated: str, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(migrated)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncQdrantClient(url=QURL, api_key=QKEY, check_compatibility=False, timeout=10)
    store = QdrantVectorStore(
        client, prefix="aics_test_", embed_model=f"it-{uuid.uuid4().hex[:8]}", dim=DIM
    )
    storage = LocalStorage(tmp_path)
    async with sessions() as s:
        u = User(email=f"u{uuid.uuid4().hex[:8]}@x.io", display_name="u", password_hash="h")
        s.add(u)
        await s.flush()
        app = await AppService(s).create_app(name=f"it-{uuid.uuid4().hex[:6]}", created_by=u.id)
        await s.commit()
        app_id, user_id = app.id, u.id
    ctx: dict[str, Any] = {
        "sessions": sessions,
        "store": store,
        "storage": storage,
        "app_id": app_id,
    }
    try:
        yield ctx
    finally:
        async with sessions() as s:
            for m in (Chunk, ChunkParent, Job, Document):
                await s.execute(delete(m).where(m.app_id == app_id))
            await s.execute(delete(App).where(App.id == app_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()
        await store.drop_chunks_collection()
        await client.close()
        await engine.dispose()


def _svc(
    env: dict[str, Any], embedder: FakeEmbedder, cleanups: list | None = None
) -> IngestService:
    async def enqueue(doc_id: uuid.UUID, below: int, old: str | None) -> None:
        if cleanups is not None:
            cleanups.append((doc_id, below, old))

    return IngestService(
        env["sessions"],
        storage=env["storage"],
        store=env["store"],
        embedder=embedder,
        settings=get_settings(),
        enqueue_cleanup=enqueue,
    )


async def _new_build(
    env: dict[str, Any], doc_id: uuid.UUID | None = None, data: bytes = MD
) -> tuple[uuid.UUID, uuid.UUID]:
    """模拟 knowledge 模块（M1.5）创建/重建：分配 pending_index_version，写 job。"""
    async with env["sessions"]() as s:
        if doc_id is None:
            doc = Document(
                app_id=env["app_id"],
                title="售后手册",
                source_type="file",
                content_hash=hashlib.sha256(data).hexdigest(),
                mime_type="text/markdown",
            )
            s.add(doc)
            await s.flush()
            doc.storage_key = f"{env['app_id']}/{doc.id}/v1.md"
            await env["storage"].put(doc.storage_key, data)
        else:
            doc = await s.get(Document, doc_id)
        doc.pending_index_version = doc.index_version + 1
        doc.status = "queued"
        job = Job(app_id=env["app_id"], type="ingest", target_id=doc.id, payload={})
        s.add(job)
        await s.commit()
        return doc.id, job.id


async def _counts(env: dict[str, Any], doc_id: uuid.UUID) -> tuple[int, int, int]:
    async with env["sessions"]() as s:
        c = (
            await s.execute(
                select(func.count()).select_from(Chunk).where(Chunk.document_id == doc_id)
            )
        ).scalar_one()
        p = (
            await s.execute(
                select(func.count())
                .select_from(ChunkParent)
                .where(ChunkParent.document_id == doc_id)
            )
        ).scalar_one()
    q = await env["store"].count_chunks(env["app_id"], enabled_only=False)
    return c, p, q


async def _set_chunk_config(env: dict[str, Any], patch: dict[str, Any]) -> None:
    async with env["sessions"]() as s:
        await AppService(s).update_app(env["app_id"], {"chunk_config": patch})
        await s.commit()


async def test_build_done_counts_match_and_rerun_is_idempotent(env: dict[str, Any]) -> None:
    emb = FakeEmbedder()
    cleanups: list = []
    doc_id, job_id = await _new_build(env)
    assert await _svc(env, emb, cleanups).run_build(doc_id, job_id) == "done"
    async with env["sessions"]() as s:
        doc = await s.get(Document, doc_id)
        job = await s.get(Job, job_id)
    assert (doc.status, doc.index_version, doc.pending_index_version, doc.progress) == (
        "done",
        1,
        None,
        100,
    )
    assert doc.indexed_title == "售后手册" and doc.embed_model == get_settings().embed_model
    assert job.status == "done" and job.finished_at is not None
    c, _, q = await _counts(env, doc_id)
    assert c == doc.chunk_count == q > 1
    assert emb.texts[0].startswith("文档：售后手册\n章节：售后 > 退货\n")
    async with env["sessions"]() as s:
        first = (
            (
                await s.execute(
                    select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal)
                )
            )
            .scalars()
            .first()
        )
    assert not first.text.startswith("文档：") and first.search_text  # 存原文；分词索引已写
    assert cleanups == [(doc_id, 1, None)]

    # 重复投递同一 job：终态直接跳过
    assert await _svc(env, emb).run_build(doc_id, job_id) == "skipped"
    # 同一构建号中断后重跑（job 回到 running）：先清半成品，结果一致
    async with env["sessions"]() as s:
        d = await s.get(Document, doc_id)
        d.pending_index_version, d.index_version, d.status = 1, 0, "indexing"
        j = await s.get(Job, job_id)
        j.status = "running"
        await s.commit()
    assert await _svc(env, FakeEmbedder()).run_build(doc_id, job_id) == "done"
    assert await _counts(env, doc_id) == (c, 0, q)


async def test_failure_keeps_old_version(env: dict[str, Any]) -> None:
    await _set_chunk_config(env, {"chunk_size": 100, "chunk_overlap": 0})  # 多批，第 2 批失败
    doc_id, job_id = await _new_build(env)
    assert await _svc(env, FakeEmbedder()).run_build(doc_id, job_id) == "done"
    before = await _counts(env, doc_id)
    assert before[0] > 8
    doc_id, job2 = await _new_build(env, doc_id)
    assert await _svc(env, FakeEmbedder(fail_on_call=2)).run_build(doc_id, job2) == "failed"
    async with env["sessions"]() as s:
        doc = await s.get(Document, doc_id)
        job = await s.get(Job, job2)
    assert doc.status == "failed" and "embedding upstream down" in doc.error
    assert doc.index_version == 1 and doc.pending_index_version is None
    assert job.status == "failed"
    assert await _counts(env, doc_id) == before  # 半成品已清，旧版本仍可检索
    hits = await env["store"].search_chunks(env["app_id"], [0.5] * DIM, limit=3)
    assert hits and all(h.index_version == 1 for h in hits)


async def test_parent_child_writes_parents_and_payload_parent_id(env: dict[str, Any]) -> None:
    await _set_chunk_config(env, {"mode": "parent_child", "parent_size": 400, "child_size": 100})
    doc_id, job_id = await _new_build(env)
    assert await _svc(env, FakeEmbedder()).run_build(doc_id, job_id) == "done"
    c, p, q = await _counts(env, doc_id)
    assert p > 1 and c == q > p
    async with env["sessions"]() as s:
        parent_ids = set(
            (
                await s.execute(select(ChunkParent.id).where(ChunkParent.document_id == doc_id))
            ).scalars()
        )
        child_parents = set(
            (await s.execute(select(Chunk.parent_id).where(Chunk.document_id == doc_id))).scalars()
        )
    assert child_parents == parent_ids
    pts, _ = await env["store"].client.scroll(
        env["store"].chunks_collection,
        scroll_filter=qm.Filter(
            must=[qm.FieldCondition(key="app_id", match=qm.MatchValue(value=str(env["app_id"])))]
        ),
        limit=500,
        with_payload=True,
    )
    assert pts and {uuid.UUID(pt.payload["parent_id"]) for pt in pts} == parent_ids


async def test_cancel_when_deleting_and_cleanup(env: dict[str, Any]) -> None:
    doc_id, job_id = await _new_build(env)
    async with env["sessions"]() as s:
        (await s.get(Document, doc_id)).status = "deleting"
        await s.commit()
    assert await _svc(env, FakeEmbedder()).run_build(doc_id, job_id) == "cancelled"

    # 正常两次构建后，清理旧版本
    async with env["sessions"]() as s:
        (await s.get(Document, doc_id)).status = "queued"
        await s.commit()
    doc_id, j1 = await _new_build(env, doc_id)
    await _svc(env, FakeEmbedder()).run_build(doc_id, j1)
    one = await _counts(env, doc_id)
    doc_id, j2 = await _new_build(env, doc_id)
    await _svc(env, FakeEmbedder()).run_build(doc_id, j2)
    assert (await _counts(env, doc_id))[0] == one[0] * 2  # 清理前新旧并存
    svc = _svc(env, FakeEmbedder())
    assert await svc.run_cleanup(doc_id, below_index_version=2) == "cleaned"
    assert await _counts(env, doc_id) == one

    # 删除：deleting 状态下 cleanup 删全部与文档行、文件
    async with env["sessions"]() as s:
        d = await s.get(Document, doc_id)
        d.status, key = "deleting", d.storage_key
        await s.commit()
    assert await svc.run_cleanup(doc_id) == "deleted"
    assert await _counts(env, doc_id) == (0, 0, 0)
    assert not await env["storage"].exists(key)
    async with env["sessions"]() as s:
        assert await s.get(Document, doc_id) is None
