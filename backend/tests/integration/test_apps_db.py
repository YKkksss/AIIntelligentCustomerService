"""apps.service 与新表 repository 在真实 PG 上的行为（迁移到 head 后运行）。

运行：AICS_TEST_DB_URL=postgresql+asyncpg://aics:<pwd>@<host>:5432/aics_test uv run pytest -m integration
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from aics.core.errors import ValidationAppError
from aics.db.models import Chunk, Document, Job, User
from aics.modules.apps.repository import AppRepository, MemberRepository
from aics.modules.apps.service import AppNotFoundError, AppService
from aics.modules.knowledge.repository import ChunkRepository, DocumentRepository, JobRepository

pytestmark = pytest.mark.integration
URL = os.environ.get("AICS_TEST_DB_URL")


@pytest.fixture
def migrated(monkeypatch: pytest.MonkeyPatch) -> str:
    if not URL:
        pytest.skip("未设置 AICS_TEST_DB_URL")
    monkeypatch.setenv("AICS_DB_URL", URL)
    from aics.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    return URL


@pytest.fixture
async def session(migrated: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated)
    async with engine.connect() as conn:
        trans = await conn.begin()
        s = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield s
        finally:
            await s.close()
            await trans.rollback()  # 每个测试结束回滚，不留数据
    await engine.dispose()


async def _user(s: AsyncSession) -> User:
    u = User(email=f"u{uuid.uuid4().hex[:8]}@x.io", display_name="u", password_hash="h")
    s.add(u)
    await s.flush()
    return u


async def test_create_read_update_app(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 保存 Webhook 地址会做 SSRF 校验（M3.5 ③）；本机 DNS 把测试域名解到保留网段，这里放行
    monkeypatch.setattr(
        "aics.modules.apps.service.netguard.check_url",
        lambda url, allow, resolver=None: ["93.184.216.34"],
    )
    svc = AppService(session)
    u = await _user(session)
    app = await svc.create_app(name=" 售后 ", created_by=u.id, rag_config={"vector_top_k": 12})
    assert app.name == "售后" and app.status == "active" and app.config_version == 1
    assert app.rag_config["vector_top_k"] == 12 and app.rag_config["intent"]["enabled"] is True
    assert app.handoff_config["handoff_ai_mode"] == "continue"
    assert app.fallback_text.startswith("抱歉") and app.custom_dict == []

    cfg = await svc.get_config(app.id)
    assert cfg.effective_session.idle_close_minutes == 10

    same = await svc.update_app(app.id, {"rag_config": {"vector_top_k": 12}})
    assert not same.changed and same.app.config_version == 1

    r = await svc.update_app(
        app.id,
        {
            "rag_config": {"intent": {"min_confidence": 0.5}},
            "greeting": "你好",
            "custom_dict": ["小优会员"],
        },
    )
    assert r.changed and r.custom_dict_changed and not r.reindex_required
    assert r.app.config_version == 2 and r.app.rag_config["intent"]["clarify_threshold"] == 0.7
    assert r.app.rag_config["vector_top_k"] == 12

    r = await svc.update_app(app.id, {"chunk_config": {"chunk_size": 400}})
    assert r.reindex_required and r.app.chunk_config_version == 2 and r.app.config_version == 3

    with pytest.raises(ValidationAppError):
        await svc.update_app(app.id, {"rag_config": {"compact_trigger_tokens": 100}})
    with pytest.raises(ValidationAppError):
        await svc.update_app(app.id, {"webhook_config": {"secret_enc": "x"}})
    with pytest.raises(ValidationAppError):
        await svc.update_app(app.id, {"config_version": 9})
    r = await svc.update_app(app.id, {"webhook_config": {"url": "https://cs.example.com/h"}})
    assert r.config.webhook.public()["url"] == "https://cs.example.com/h"


async def test_not_found_and_deleting(session: AsyncSession) -> None:
    svc = AppService(session)
    with pytest.raises(AppNotFoundError):
        await svc.get_app(uuid.uuid4())
    app = await svc.create_app(name="a", created_by=None)
    await AppRepository(session).save_values(app.id, {"status": "deleting"})
    with pytest.raises(AppNotFoundError):
        await svc.get_app(app.id)
    assert (await svc.get_app(app.id, include_deleting=True)).id == app.id
    assert app.id not in [a.id for a in await svc.list_apps(ids=None)]
    assert app.id in [a.id for a in await svc.list_apps(ids=[app.id], include_deleting=True)]


async def test_create_rejects_invalid(session: AsyncSession) -> None:
    svc = AppService(session)
    with pytest.raises(ValidationAppError):
        await svc.create_app(name="a", created_by=None, rag_config={"llm_model": "nope"})
    with pytest.raises(ValidationAppError):
        await svc.create_app(name="a", created_by=None, colour="red")


async def test_tenant_isolation_across_new_tables(session: AsyncSession) -> None:
    svc = AppService(session)
    a = await svc.create_app(name="A", created_by=None)
    b = await svc.create_app(name="B", created_by=None)
    docs, chunks, jobs = (
        DocumentRepository(session),
        ChunkRepository(session),
        JobRepository(session),
    )
    d = await docs.add(
        a.id, Document(app_id=a.id, title="t", source_type="file", content_hash="0" * 64)
    )
    assert (await docs.get_by_hash(a.id, "0" * 64)) is not None
    assert (await docs.get_by_hash(b.id, "0" * 64)) is None
    assert await docs.get(b.id, d.id) is None

    c = await chunks.add(
        a.id,
        Chunk(
            app_id=a.id,
            document_id=d.id,
            index_version=1,
            ordinal=0,
            text="退货运费",
            search_text="退货 运费",
        ),
    )
    assert [x.id for x in await chunks.list_for_document(a.id, d.id, 1)] == [c.id]
    assert await chunks.list_for_document(b.id, d.id, 1) == []
    tsv = await session.execute(text("SELECT tsv::text FROM chunks WHERE id=:i"), {"i": c.id})
    assert "'退货'" in tsv.scalar_one()

    j = await jobs.add(a.id, Job(app_id=a.id, type="ingest", target_id=d.id))
    assert await jobs.get(b.id, j.id) is None and (await jobs.get(a.id, j.id)).status == "queued"
    pj = await jobs.add_platform_job_all_apps(Job(type="reindex_all"))
    assert pj.id in [x.id for x in await jobs.list_platform_all_apps()]

    apps = AppRepository(session)
    assert await apps.update(b.id, a.id, name="hack") == 0
    assert (await apps.get(a.id, a.id)).name == "A"


async def test_members(session: AsyncSession) -> None:
    svc = AppService(session)
    a = await svc.create_app(name="A", created_by=None)
    u = await _user(session)
    m = MemberRepository(session)
    await m.set_role(a.id, u.id, "viewer")
    await m.set_role(a.id, u.id, "app_admin")
    assert await m.get_role(a.id, u.id) == "app_admin"
    assert await m.count_role(a.id, "app_admin") == 1
    assert (await m.roles_for_user_all_apps(u.id)) == {a.id: "app_admin"}
    assert await m.delete(a.id, u.id) == 1 and await m.get_role(a.id, u.id) is None


async def test_constraints(session: AsyncSession) -> None:
    svc = AppService(session)
    a = await svc.create_app(name="A", created_by=None)
    docs = DocumentRepository(session)
    await docs.add(
        a.id, Document(app_id=a.id, title="t", source_type="file", content_hash="1" * 64)
    )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await docs.add(
                a.id, Document(app_id=a.id, title="t2", source_type="file", content_hash="1" * 64)
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await JobRepository(session).add(a.id, Job(app_id=a.id, type="bogus"))


async def test_chunk_preset_update_and_parent_child_rows(session: AsyncSession) -> None:
    from aics.db.models import ChunkParent
    from aics.modules.knowledge.repository import ChunkParentRepository

    svc = AppService(session)
    a = await svc.create_app(name="A", created_by=None, chunk_config={"preset": "legal"})
    assert a.chunk_config["strategy"] == "article" and a.chunk_config["chunk_size"] == 400
    r = await svc.update_app(a.id, {"chunk_config": {"chunk_overlap": 20}})
    assert r.reindex_required and r.app.chunk_config["preset"] == "legal"
    r = await svc.update_app(a.id, {"chunk_config": {"preset": "faq"}})
    assert r.app.chunk_config["chunk_overlap"] == 0 and r.app.chunk_config["strategy"] == "faq"
    r = await svc.update_app(
        a.id, {"chunk_config": {"mode": "parent_child", "parent_size": 1024, "child_size": 200}}
    )
    assert r.app.chunk_config["mode"] == "parent_child" and r.app.chunk_config_version == 4

    b = await svc.create_app(name="B", created_by=None)
    d = await DocumentRepository(session).add(
        a.id, Document(app_id=a.id, title="t", source_type="file", content_hash="2" * 64)
    )
    parents = ChunkParentRepository(session)
    p = await parents.add(
        a.id,
        ChunkParent(
            app_id=a.id, document_id=d.id, index_version=1, ordinal=0, text="父块正文", tokens=4
        ),
    )
    c = await ChunkRepository(session).add(
        a.id,
        Chunk(
            app_id=a.id,
            document_id=d.id,
            index_version=1,
            ordinal=0,
            parent_id=p.id,
            text="子块",
            search_text="子块",
        ),
    )
    assert (await parents.get_many(a.id, [p.id]))[p.id].text == "父块正文"
    assert await parents.get_many(b.id, [p.id]) == {}
    assert await parents.get_many(a.id, []) == {}
    await session.delete(p)  # 删除父块级联删除子块
    await session.flush()
    session.expunge(c)
    assert await ChunkRepository(session).get(a.id, c.id) is None
