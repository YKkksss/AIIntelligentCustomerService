"""M4.3b ③：切块级运营的集成用例（真 PG + 真 Qdrant + 本地存储 + 假嵌入器）。

覆盖只有真库能证明的四件事：

- 编辑单块正文后，只有该块的分词索引/向量变，其他块不受影响；
- 手工块（`source=manual`）在重建后迁到新构建号，仍能检索到；
- 文档有效期窗口在 `retrievable()` 里生效（过期/未生效都搜不到，且不影响其他文档）；
- 两份同文文档的 SimHash 近重复提示。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from ingest_support import FakeEmbedder
from sqlalchemy import select

from aics.core.config import get_settings
from aics.db.models import App, Chunk, Document, Job
from aics.ingest.service import IngestService
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.modules.knowledge.service import KnowledgeService

pytestmark = pytest.mark.integration

MD = (
    "# 售后手册\n\n"
    "## 退货\n\n第 1 条 自签收之日起七天内可无理由退货，运费由我们承担。\n\n"
    "## 发货\n\n第 2 条 加急订单 24 小时内发货，节假日顺延。\n"
).encode()


def _ingest(env: dict[str, Any], embedder: FakeEmbedder | None = None) -> IngestService:
    return IngestService(
        env["sessions"],
        storage=env["storage"],
        store=env["store"],
        embedder=embedder or FakeEmbedder(),
        settings=get_settings(),
    )


def _knowledge(env: dict[str, Any], *, embedder: FakeEmbedder | None = None) -> KnowledgeService:
    return KnowledgeService(
        None,  # type: ignore[arg-type]  # 每个方法自己开会话的入口见下方 _with_session
        storage=env["storage"],
        store=env["store"],
        embedder=embedder or FakeEmbedder(),
        enqueue=lambda task, args: None,
    )


async def _new_document(env: dict[str, Any], data: bytes = MD, title: str = "售后手册") -> Document:
    async with env["sessions"]() as s:
        doc = Document(
            app_id=env["app_id"],
            title=title,
            source_type="file",
            content_hash=hashlib.sha256(data).hexdigest(),
            mime_type="text/markdown",
        )
        s.add(doc)
        await s.flush()
        doc.storage_key = f"{env['app_id']}/{doc.id}/v1.md"
        await env["storage"].put(doc.storage_key, data)
        doc.pending_index_version = 1
        job = Job(app_id=env["app_id"], type="ingest", target_id=doc.id, payload={})
        s.add(job)
        await s.commit()
        return doc, job.id


async def _build(env: dict[str, Any], doc: Document, job_id: uuid.UUID) -> None:
    status = await _ingest(env).run_build(doc.id, job_id)
    if status != "done":
        async with env["sessions"]() as s:
            job = await s.get(Job, job_id)
        raise AssertionError(f"build {status}: {job.error if job else '?'}")


async def _rebuild(env: dict[str, Any], doc_id: uuid.UUID) -> None:
    async with env["sessions"]() as s:
        doc = await s.get(Document, doc_id)
        doc.pending_index_version = doc.index_version + 1
        doc.status = "queued"
        job = Job(app_id=env["app_id"], type="ingest", target_id=doc_id, payload={})
        s.add(job)
        await s.commit()
        version, job_id = doc.pending_index_version, job.id
    assert version == 2
    assert await _ingest(env).run_build(doc_id, job_id) == "done"


async def _with_service(env: dict[str, Any], fn: Any) -> Any:
    """`KnowledgeService` 绑定会话后执行一个动作（生产里由 runtime 提供会话）。"""
    async with env["sessions"]() as s:
        service = KnowledgeService(
            s,
            storage=env["storage"],
            store=env["store"],
            embedder=FakeEmbedder(),
            enqueue=lambda task, args: None,
        )
        return await fn(service)


async def _chunks(
    env: dict[str, Any], doc_id: uuid.UUID, version: int | None = None
) -> list[Chunk]:
    async with env["sessions"]() as s:
        doc = await s.get(Document, doc_id)
        stmt = select(Chunk).where(
            Chunk.document_id == doc_id,
            Chunk.index_version == (version if version is not None else doc.index_version),
        )
        return list((await s.scalars(stmt)).all())


async def test_editing_one_chunk_only_touches_that_chunk(env: dict[str, Any]) -> None:
    doc, job_id = await _new_document(env)
    await _build(env, doc, job_id)
    rows = await _chunks(env, doc.id)
    assert len(rows) >= 2
    target = rows[0]
    others = {row.id: (row.text, row.search_text, row.tokens) for row in rows[1:]}

    edited = await _with_service(
        env,
        lambda svc: svc.update_chunk(
            env["app_id"],
            target.id,
            text="签收后十五天内可无理由退货，运费由我们承担。",
        ),
    )

    assert edited.source == "edited"
    assert edited.search_text != target.search_text or edited.text == target.text
    after = {row.id: (row.text, row.search_text, row.tokens) for row in await _chunks(env, doc.id)}
    assert set(after) == {target.id, *others}
    for chunk_id, values in others.items():
        assert after[chunk_id] == values  # 其他块的正文与分词索引没被动过

    hits = await PgChunkSearch(env["sessions"]).keyword_search(env["app_id"], "十五天", 5)
    assert target.id in {hit.chunk_id for hit in hits}
    old_hits = await PgChunkSearch(env["sessions"]).keyword_search(env["app_id"], "七天", 5)
    assert target.id not in {hit.chunk_id for hit in old_hits}


async def test_manual_chunk_survives_rebuild(env: dict[str, Any]) -> None:
    doc, job_id = await _new_document(env)
    await _build(env, doc, job_id)

    manual = await _with_service(
        env,
        lambda svc: svc.add_chunk(
            env["app_id"],
            doc.id,
            text="加急订单请在备注中填写期望送达时间，我们会优先安排。",
        ),
    )
    assert manual.source == "manual" and manual.index_version == 1

    await _rebuild(env, doc.id)

    version_two = await _chunks(env, doc.id)
    carried = [row for row in version_two if row.source == "manual"]
    assert len(carried) == 1
    assert carried[0].index_version == 2
    assert carried[0].text == "加急订单请在备注中填写期望送达时间，我们会优先安排。"
    assert carried[0].id != manual.id  # 新构建号下是新行

    hits = await PgChunkSearch(env["sessions"]).keyword_search(env["app_id"], "加急", 5)
    assert carried[0].id in {hit.chunk_id for hit in hits}


async def test_expired_document_leaves_the_retrievable_set(env: dict[str, Any]) -> None:
    doc, job_id = await _new_document(env)
    await _build(env, doc, job_id)
    search = PgChunkSearch(env["sessions"])
    assert await search.keyword_search(env["app_id"], "退货", 5)

    async with env["sessions"]() as s:
        row = await s.get(Document, doc.id)
        row.expires_at = row.created_at  # 已过期（created_at 在过去的测试时钟里）
        row.effective_at = None
        await s.commit()

    assert await search.keyword_search(env["app_id"], "退货", 5) == []


async def test_future_document_is_not_retrievable_yet(env: dict[str, Any]) -> None:
    from datetime import UTC, datetime, timedelta

    doc, job_id = await _new_document(env)
    await _build(env, doc, job_id)
    async with env["sessions"]() as s:
        row = await s.get(Document, doc.id)
        row.effective_at = datetime.now(UTC) + timedelta(days=1)
        await s.commit()

    search = PgChunkSearch(env["sessions"])
    assert await search.keyword_search(env["app_id"], "退货", 5) == []


async def test_near_duplicate_documents_are_reported(env: dict[str, Any]) -> None:
    first, job_a = await _new_document(env, title="售后手册")
    await _build(env, first, job_a)
    # 只差空行：content_hash 不同（不会被入库去重拦下），SimHash 相同
    second, job_b = await _new_document(env, data=MD + b"`n", title="售后手册（副本）")
    await _build(env, second, job_b)

    duplicates = await _with_service(env, lambda svc: svc.duplicates(env["app_id"], second.id))

    assert [item["title"] for item in duplicates] == ["售后手册"]
    assert duplicates[0]["distance"] <= 3
    # 反向查询同样命中，且都是"提示"而不是阻止
    reverse = await _with_service(env, lambda svc: svc.duplicates(env["app_id"], first.id))
    assert [item["title"] for item in reverse] == ["售后手册（副本）"]


async def test_app_custom_dict_does_not_break_new_chunks(env: dict[str, Any]) -> None:
    async with env["sessions"]() as s:
        await s.execute(
            App.__table__.update().where(App.id == env["app_id"]).values(custom_dict=["退货"])
        )
        await s.commit()
    doc, job_id = await _new_document(env)
    await _build(env, doc, job_id)

    added = await _with_service(
        env, lambda svc: svc.add_chunk(env["app_id"], doc.id, text="退货需原包装")
    )

    assert added.search_text and "退货" in added.search_text
