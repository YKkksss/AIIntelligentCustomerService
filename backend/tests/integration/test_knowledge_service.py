"""M1.6 知识库服务 + M1 里程碑验收：真实 PG + Qdrant（aics_test_ 前缀），任务在进程内按投递顺序执行。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from ingest_support import DIM, MD, FakeEmbedder
from sqlalchemy import func, select
from test_ingest_pipeline import _counts

from aics.core.config import get_settings
from aics.db.models import Chunk, Document, Job
from aics.ingest.parsers.url import FetchedUrl
from aics.ingest.service import IngestService
from aics.modules.knowledge.service import (
    DocumentBusyError,
    DuplicateDocumentError,
    KnowledgeService,
)
from aics.vectorstore import ChunkPoint

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "docs"


class Harness:
    """收集 KnowledgeService / 构建投递的任务，在进程内执行（等价于 worker 消费）。"""

    def __init__(self, env: dict[str, Any], embedder: FakeEmbedder | None = None) -> None:
        self.env = env
        self.queue: list[tuple[str, list[Any]]] = []
        self.embedder = embedder or FakeEmbedder()
        self.fetches: list[str] = []

    def enqueue(self, task: str, args: list[Any]) -> None:
        self.queue.append((task, list(args)))

    async def fetch(self, url: str) -> FetchedUrl:
        self.fetches.append(url)
        html = (
            b"<html><head><title>Return Policy</title></head><body><h1>Returns</h1>"
            + (
                b"<p>Items can be returned within seven days of delivery for a full refund.</p>"
                * 30
            )
            + b"</body></html>"
        )
        return FetchedUrl(url, html, "text/html", "html")

    def ingest(self) -> IngestService:
        def cleanup(doc: uuid.UUID, below: int, old: str | None) -> None:
            self.enqueue("aics.cleanup_document", [str(doc), below, old])

        return IngestService(
            self.env["sessions"],
            storage=self.env["storage"],
            store=self.env["store"],
            embedder=self.embedder,
            settings=get_settings(),
            enqueue_cleanup=cleanup,
        )

    def ks(self, s: Any) -> KnowledgeService:
        return KnowledgeService(
            s,
            storage=self.env["storage"],
            store=self.env["store"],
            enqueue=self.enqueue,
            fetcher=self.fetch,
        )

    async def call(self, fn: str, *args: Any, **kw: Any) -> Any:
        async with self.env["sessions"]() as s:
            return await getattr(self.ks(s), fn)(*args, **kw)

    async def drain(self, *, only: str | None = None) -> list[str]:
        out = []
        while self.queue:
            idx = next((i for i, (t, _) in enumerate(self.queue) if only in (None, t)), None)
            if idx is None:
                break
            task, a = self.queue.pop(idx)
            svc = self.ingest()
            if task == "aics.ingest_document":
                out.append(await svc.run_build(uuid.UUID(a[0]), uuid.UUID(a[1])))
            elif task == "aics.cleanup_document":
                job = uuid.UUID(a[3]) if len(a) > 3 and a[3] else None
                out.append(
                    await svc.run_cleanup(
                        uuid.UUID(a[0]), below_index_version=a[1], old_storage_key=a[2], job_id=job
                    )
                )
            elif task == "aics.retokenize_app":
                out.append(await svc.run_retokenize(uuid.UUID(a[0]), uuid.UUID(a[1])))
            else:  # pragma: no cover
                raise AssertionError(task)
        return out

    async def retrievable(self) -> int:
        async with self.env["sessions"]() as s:
            return await self.ks(s).retrievable_chunk_count(self.env["app_id"])

    async def doc(self, doc_id: uuid.UUID) -> Document | None:
        async with self.env["sessions"]() as s:
            return await s.get(Document, doc_id)

    async def reconcile(self) -> dict[str, Any]:
        return await self.ingest().reconcile_app(self.env["app_id"])


async def test_m1_upload_three_formats_to_done_and_reconcile_clean(env: dict[str, Any]) -> None:
    h = Harness(env)
    ids = []
    for name in ("sample.md", "sample.docx", "sample.pdf"):
        r = await h.call("upload_file", env["app_id"], (FIXTURES / name).read_bytes(), name)
        assert not r.duplicate and r.job.type == "ingest"
        ids.append(r.document.id)
    assert await h.drain() == ["done"] * 3 + ["cleaned"] * 3  # 首次构建的清理为空操作
    for i in ids:
        d = await h.doc(i)
        assert d.status == "done" and d.index_version == 1 and d.chunk_count > 0
    rep = await h.reconcile()
    assert rep["diffs"] == [] and rep["pg"] == rep["qdrant"] == await h.retrievable() > 0
    dup = await h.call("upload_file", env["app_id"], (FIXTURES / "sample.md").read_bytes(), "x.md")
    assert dup.duplicate and dup.document.id == ids[0]


async def _mid_build_probe(h: Harness, expected_old: int) -> list[int]:
    """嵌入器第一次调用时（新版本切块已写入 PG/Qdrant）记录可检索数。"""
    seen: list[int] = []
    inner = h.embedder

    class Probe(FakeEmbedder):
        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            if not seen:
                seen.append(await h.retrievable())
            return await inner.embed_documents(texts)

    h.embedder = Probe()
    return seen


async def test_replace_keeps_old_version_until_switch(env: dict[str, Any]) -> None:
    h = Harness(env)
    r = await h.call("upload_file", env["app_id"], MD, "policy.md")
    await h.drain()
    doc_id, old_key, old = r.document.id, r.document.storage_key, await h.retrievable()

    # 规则 1：内容相同 → unchanged，不建任务
    same = await h.call("replace_file", env["app_id"], doc_id, MD, "policy.md")
    assert same.unchanged and same.job is None and not h.queue
    # 规则 2：与另一文档相同 → 409 DUPLICATE_DOCUMENT
    other = await h.call("upload_file", env["app_id"], b"# other\n\nanother doc body text", "o.md")
    await h.drain()
    with pytest.raises(DuplicateDocumentError) as ei:
        await h.call(
            "replace_file", env["app_id"], doc_id, b"# other\n\nanother doc body text", "o.md"
        )
    assert ei.value.details == {"document_id": str(other.document.id)}

    old = await h.retrievable()  # 含 other
    new = MD.replace("七天".encode(), "十五天".encode())
    rep = await h.call("replace_file", env["app_id"], doc_id, new, "policy-v2.md")
    assert not rep.unchanged and rep.job.type == "replace"
    assert rep.job.payload["storage_key"].endswith("/v2.md")
    with pytest.raises(DocumentBusyError):  # 构建中不能再替换 / 重建
        await h.call("replace_file", env["app_id"], doc_id, new + b"x", "p.md")
    with pytest.raises(DocumentBusyError):
        await h.call("rebuild_document", env["app_id"], doc_id)

    seen = await _mid_build_probe(h, old)
    assert await h.drain() == ["done", "cleaned"]
    assert seen == [old]  # 构建期间旧版本仍可检索（新版本切块已写入但不计入）
    d = await h.doc(doc_id)
    assert (d.version, d.index_version, d.storage_key) == (2, 2, rep.job.payload["storage_key"])
    assert d.content_hash == rep.job.payload["content_hash"] and d.id == doc_id
    assert not await env["storage"].exists(old_key) and await env["storage"].exists(d.storage_key)
    async with env["sessions"]() as s:
        txt = (
            (await s.execute(select(Chunk.text).where(Chunk.document_id == doc_id))).scalars().all()
        )
    assert txt and all("十五天" in t for t in txt if "天" in t)
    assert (await h.reconcile())["diffs"] == []


async def test_rebuild_document_and_app(env: dict[str, Any]) -> None:
    h = Harness(env)
    a = await h.call("upload_file", env["app_id"], MD, "a.md")
    b = await h.call("upload_file", env["app_id"], MD + b"\n\nextra", "b.md")
    await h.drain()
    old = await h.retrievable()
    job = await h.call("rebuild_document", env["app_id"], a.document.id)
    assert job.type == "reindex_document"
    seen = await _mid_build_probe(h, old)
    assert await h.drain() == ["done", "cleaned"]
    assert seen == [old]
    assert (await h.doc(a.document.id)).index_version == 2

    # 应用全部重建：构建中的文档跳过
    await h.call("rebuild_document", env["app_id"], b.document.id)
    res = await h.call("rebuild_app", env["app_id"])
    assert [j.target_id for j in res.jobs] == [a.document.id] and res.skipped == [b.document.id]
    assert res.job.type == "reindex_app" and res.job.status == "done"
    assert sorted(await h.drain()) == ["cleaned", "cleaned", "done", "done"]
    assert (await h.doc(a.document.id)).index_version == 3
    assert (await h.doc(b.document.id)).index_version == 2
    assert await h.retrievable() == old and (await h.reconcile())["diffs"] == []

    # 失败后重试（重建）：新构建号
    async with env["sessions"]() as s:
        d = await s.get(Document, b.document.id)
        d.status = "failed"
        await s.commit()
    j = await h.call("rebuild_document", env["app_id"], b.document.id)
    assert (await h.doc(b.document.id)).pending_index_version == 3 and j.status == "queued"


async def test_delete_immediately_unretrievable_then_cleaned(env: dict[str, Any]) -> None:
    h = Harness(env)
    r = await h.call("upload_file", env["app_id"], MD, "a.md")
    keep = await h.call("upload_file", env["app_id"], b"# keep\n\nkeep this document", "k.md")
    await h.drain()
    doc_id, key = r.document.id, r.document.storage_key
    keep_n = (await _counts(env, keep.document.id))[0]
    job = await h.call("delete_document", env["app_id"], doc_id)
    assert job.type == "cleanup_document"
    # 立即：PG 不可检索；Qdrant 该文档点已 enabled=false
    assert await h.retrievable() == keep_n
    by_doc = await env["store"].count_by_document(env["app_id"], enabled_only=True)
    assert doc_id not in by_doc and by_doc[keep.document.id] == keep_n
    with pytest.raises(DocumentBusyError):
        await h.call("delete_document", env["app_id"], doc_id)
    # 删除中（deleted_at 已写）可重新上传相同内容：成为新文档
    again = await h.call("upload_file", env["app_id"], MD, "a.md")
    assert not again.duplicate and again.document.id != doc_id
    assert await h.drain(only="aics.cleanup_document") == ["deleted"]
    assert await h.doc(doc_id) is None and not await env["storage"].exists(key)
    assert doc_id not in await env["store"].count_by_document(env["app_id"], enabled_only=False)
    async with env["sessions"]() as s:
        assert (await s.get(Job, job.id)).status == "done"
        n = (
            await s.execute(
                select(func.count()).select_from(Chunk).where(Chunk.document_id == doc_id)
            )
        ).scalar_one()
    assert n == 0


async def test_delete_during_build_cancels(env: dict[str, Any]) -> None:
    h = Harness(env)
    r = await h.call("upload_file", env["app_id"], MD, "a.md")
    await h.call("delete_document", env["app_id"], r.document.id)
    assert await h.drain() == ["cancelled", "deleted"]
    assert await h.doc(r.document.id) is None
    assert await env["store"].count_chunks(env["app_id"], enabled_only=False) == 0


async def test_url_import_snapshot_and_dedup(env: dict[str, Any]) -> None:
    h = Harness(env)
    r = await h.call("import_url", env["app_id"], "https://example.com/returns")
    d = r.document
    assert (
        not r.duplicate and d.source_type == "url" and d.source_uri == "https://example.com/returns"
    )
    assert (
        d.title == "Return Policy"
        and d.mime_type == "text/html"
        and d.storage_key.endswith("/v1.html")
    )
    assert await h.drain() == ["done", "cleaned"]
    # 同内容再次导入（另一个地址也一样）→ 返回已有文档
    again = await h.call("import_url", env["app_id"], "https://example.com/returns?utm=1")
    assert again.duplicate and again.document.id == d.id
    # 重建用快照，不重新抓取
    await h.call("rebuild_document", env["app_id"], d.id)
    assert await h.drain() == ["done", "cleaned"] and len(h.fetches) == 2


async def test_reconcile_detects_and_sweep_repairs(env: dict[str, Any]) -> None:
    h = Harness(env)
    r = await h.call("upload_file", env["app_id"], MD, "a.md")
    await h.drain()
    doc_id = r.document.id
    store = env["store"]
    # 1) 孤儿点：Qdrant 有、PG 无
    ghost = uuid.uuid4()
    await store.upsert_chunks(env["app_id"], [ChunkPoint(uuid.uuid4(), ghost, 1, [0.3] * DIM)])
    # 2) 不一致：删掉一个真实点
    async with env["sessions"]() as s:
        cid = (
            await s.execute(select(Chunk.id).where(Chunk.document_id == doc_id).limit(1))
        ).scalar_one()
    await store.client.delete(store.chunks_collection, points_selector=[str(cid)], wait=True)
    rep = await h.reconcile()
    kinds = {(x["document_id"], x["kind"]) for x in rep["diffs"]}
    assert kinds == {(str(ghost), "orphan"), (str(doc_id), "mismatch")}

    # 3) 旧版本遗留（清理任务丢失）
    await h.call("rebuild_document", env["app_id"], doc_id)
    assert await h.drain(only="aics.ingest_document") == ["done"]
    h.queue.clear()  # 丢掉 cleanup
    assert (await h.reconcile())["skipped_documents"] == 1
    counts = await h.ingest().run_sweep()
    assert counts == {"deleted": 0, "old_versions": 1, "orphans": 1}
    assert ghost not in await store.count_by_document(env["app_id"], enabled_only=False)
    assert (await h.reconcile())["diffs"] == []  # 重建后的新版本完整，旧版本已清

    # 4) 删除超时未完成 → sweep 补做
    await h.call("delete_document", env["app_id"], doc_id)
    h.queue.clear()
    assert (await h.ingest().run_sweep(stale_after_s=0))["deleted"] == 1
    assert await h.doc(doc_id) is None
