"""M1.7 文档级 chunk_config 覆盖：上传 / 替换 / 重建携带、修改只重建该文档、应用级重建跳过覆盖文档。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from test_knowledge_service import Harness

from aics.core.errors import ValidationAppError
from aics.db.models import App, Chunk, Document

pytestmark = pytest.mark.integration

BODY = "".join(
    f"## 第{i}节\n\n"
    + "签收后七天内商品完好可申请无理由退货。质量问题运费由商家承担。" * 12
    + "\n\n"
    for i in range(1, 7)
)
MD_A = ("# 售后政策 A\n\n" + BODY).encode()
MD_B = ("# 售后政策 B\n\n" + BODY.replace("七天", "十五天")).encode()


async def _chunks(h: Harness, doc_id: Any) -> int:
    async with h.env["sessions"]() as s:
        doc = await s.get(Document, doc_id)
        return int(
            await s.scalar(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.document_id == doc_id, Chunk.index_version == doc.index_version)
            )
            or 0
        )


async def test_upload_with_override_and_rebuild_app_skips_it(env: dict[str, Any]) -> None:
    h = Harness(env)
    app_id = env["app_id"]
    small = {"chunk_size": 120, "chunk_overlap": 20}
    a = await h.call("upload_file", app_id, MD_A, "a.md", chunk_config=small)
    b = await h.call("upload_file", app_id, MD_B, "b.md")
    assert [x for x in await h.drain() if x != "cleaned"] == ["done", "done"]
    da, db = await h.doc(a.document.id), await h.doc(b.document.id)
    assert da.chunk_config["chunk_size"] == 120 and da.chunk_config["preset"] == "general"
    assert db.chunk_config is None
    assert await _chunks(h, da.id) > await _chunks(h, db.id)

    r = await h.call("rebuild_app", app_id)
    assert r.skipped_override == [da.id] and [j.target_id for j in r.jobs] == [db.id]
    await h.drain()
    r = await h.call("rebuild_app", app_id, include_overridden=True)
    assert {j.target_id for j in r.jobs} == {da.id, db.id}
    await h.drain()
    assert (await h.reconcile())["diffs"] == []


async def test_set_override_rebuilds_only_that_doc_and_reset(env: dict[str, Any]) -> None:
    h = Harness(env)
    app_id = env["app_id"]
    a = await h.call("upload_file", app_id, MD_A, "a.md")
    b = await h.call("upload_file", app_id, MD_B, "b.md")
    await h.drain()
    before_b = (await h.doc(b.document.id)).index_version
    general = await _chunks(h, a.document.id)

    job = await h.call(
        "set_document_chunk_config", app_id, a.document.id, {"chunk_size": 120, "chunk_overlap": 20}
    )
    assert job is not None and job.type == "reindex_document"
    assert len(h.queue) == 1  # 只投递该文档
    assert (await h.doc(a.document.id)).chunk_config is None  # 构建成功前不生效
    assert [x for x in await h.drain() if x != "cleaned"] == ["done"]
    da = await h.doc(a.document.id)
    assert da.chunk_config["chunk_size"] == 120 and await _chunks(h, da.id) > general
    assert (await h.doc(b.document.id)).index_version == before_b

    # 相同配置再次设置：不重建
    assert (
        await h.call(
            "set_document_chunk_config",
            app_id,
            a.document.id,
            {"chunk_size": 120, "chunk_overlap": 20},
        )
        is None
    )
    # 预设：以预设为底
    await h.call("set_document_chunk_config", app_id, a.document.id, {"preset": "legal"})
    await h.drain()
    assert (await h.doc(a.document.id)).chunk_config["strategy"] == "article"
    # 恢复跟随应用
    job = await h.call("set_document_chunk_config", app_id, a.document.id, None)
    assert job is not None
    await h.drain()
    da = await h.doc(a.document.id)
    assert da.chunk_config is None and await _chunks(h, da.id) == general

    # 与应用配置相同的覆盖：只固定、不重建
    async with env["sessions"]() as s:
        app_cfg = (await s.get(App, app_id)).chunk_config
    assert await h.call("set_document_chunk_config", app_id, a.document.id, dict(app_cfg)) is None
    assert (await h.doc(a.document.id)).chunk_config == app_cfg


async def test_replace_and_rebuild_carry_override_and_validation(env: dict[str, Any]) -> None:
    h = Harness(env)
    app_id = env["app_id"]
    a = await h.call("upload_file", app_id, MD_A, "a.md")
    await h.drain()
    r = await h.call(
        "replace_file", app_id, a.document.id, MD_B, "b.md", chunk_config={"chunk_size": 200}
    )
    assert r.job.payload["chunk_config_override"]["chunk_size"] == 200
    await h.drain()
    da = await h.doc(a.document.id)
    assert da.version == 2 and da.chunk_config["chunk_size"] == 200
    # 内容不变、只改配置：按配置变更重建
    r = await h.call(
        "replace_file", app_id, a.document.id, MD_B, "b.md", chunk_config={"chunk_size": 300}
    )
    assert not r.unchanged and r.job is not None
    await h.drain()
    job = await h.call("rebuild_document", app_id, a.document.id, reset_chunk_config=True)
    assert job.payload == {"chunk_config_override": None}
    await h.drain()
    assert (await h.doc(a.document.id)).chunk_config is None

    for bad in ({"chunk_size": 50}, {"preset": "nope"}, {"chunk_overlap": 400}):
        with pytest.raises(ValidationAppError):
            await h.call("upload_file", app_id, MD_A + b"x", "c.md", chunk_config=bad)
    with pytest.raises(ValidationAppError):
        await h.call(
            "rebuild_document", app_id, a.document.id, chunk_config={}, reset_chunk_config=True
        )


async def test_app_rag_update_checks_doc_override_window(env: dict[str, Any]) -> None:
    from aics.core.config import get_settings
    from aics.core.models import ModelRegistry
    from aics.modules.apps.service import AppService

    h = Harness(env)
    app_id = env["app_id"]
    big = {"mode": "parent_child", "parent_mode": "full_doc", "full_doc_max_tokens": 10000}
    await h.call("upload_file", app_id, MD_A, "a.md", chunk_config=big)
    await h.call("upload_file", app_id, MD_B, "b.md")  # 无覆盖：必须是 SQL NULL 而非 JSON null
    await h.drain()
    async with env["sessions"]() as s:
        n = await s.scalar(
            select(func.count()).select_from(Document).where(Document.chunk_config.is_(None))
        )
    assert n == 1
    reg = ModelRegistry.model_validate(
        [
            {**m.model_dump(), "context_window": 128_000} if m.kind == "chat" else m.model_dump()
            for m in get_settings().models.root
        ]
    )
    settings = get_settings().model_copy(update={"models": reg})
    async with env["sessions"]() as s:
        svc = AppService(s, settings=settings)
        await svc.update_app(app_id, {"rag_config": {"rerank_top_n": 5}})  # 5 × 10000 可以
        with pytest.raises(ValidationAppError) as ei:
            await svc.update_app(app_id, {"rag_config": {"rerank_top_n": 20}})
    assert "文档级" in str(ei.value.details)
